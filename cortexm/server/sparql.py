"""SPARQL endpoint — non-LLM decoder path (NSR-inspired, v2 extended).

arXiv insight (NSR / ESWEEK24): the VSA core is task-agnostic; only
the decoder changes. Context-M's reader was hardcoded to format facts
for an LLM prompt. The Decoders module extracts that formatter into
a pluggable interface — `RDFDecoder` exports facts as RDF/N3 triples.

This module exposes a SPARQL HTTP endpoint that:
  1. accepts SPARQL SELECT queries via HTTP GET/POST
  2. parses the query (a hand-rolled parser covering the common
     subset: SELECT [DISTINCT] ?vars WHERE { triple-patterns +
     FILTERs + OPTIONAL } ORDER BY ?var [ASC|DESC] LIMIT N)
  3. retrieves facts from Context-M's Memory.store (or its
     RDFDecoder if attached — same substrate either way)
  4. runs the WHERE clause as a join pipeline over those triples
     — naive nested-loop join with binding propagation, OPTIONAL
     handled via LEFT-JOIN semantics, FILTER applied per binding
  5. resolves blob-arena-stored objects on demand (the sidecar
     blob arena is the Aeon off-graph store; SPARQL "o" may
     transparently dereference long text via Memory.get_chunk_text)
  6. exposes CAUSAL / REFERS_TO typed edges (Aeon) via the
     `edge/2` predicate family so external graph tools can walk
     the truth-maintenance and episodic-atlas graphs natively
  7. returns the result as SPARQL 1.1 JSON Results

This is a NON-LLM retrieval path. Zero LLM calls. The same palace +
Trace substrate that powers LLM context-stuffing now serves SPARQL.

Usage:
    # standalone SPARQL endpoint (e.g. on port 8910)
    python -m cortexm.server.sparql --port 8910

    # launched alongside the REST API (recommended for production):
    # `cortexm serve-rest --sparql-port 8910` — both share one Memory
    # instance; the REST API also exposes /v1/sparql for unified access.

Example queries (v2 supports a much richer subset than v1):
    SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 10
    SELECT DISTINCT ?s WHERE { ?s ?p ?o } ORDER BY ?s
    SELECT ?s ?o WHERE { ?s "name" ?o . FILTER regex(?o, "^Jen", "i") }
    SELECT ?cause ?effect WHERE {
        ?cause edge:CAUSAL ?effect .
        ?effect "name" "Alice"
    } LIMIT 5

LIMITATIONS (honest, documented):
  - Parser covers the SELECT subset named above. UNION, CONSTRUCT,
    ASK, DESCRIBE, property paths (rdf:type/rdfs:subClassOf*), and
    SPARQL 1.1 aggregates (GROUP BY / COUNT / SUM) are NOT yet
    supported. For those, export via RDFDecoder + use Apache Jena.
  - No inference / reasoning over RDFS/OWL — pattern matching on
    the actual stored facts only.
  - Joins are nested-loop (no query planner). Adequate for the
    ~10^5 facts Context-M is designed to hold per user; for larger
    stores, use a real triple store fed via the RDFDecoder export.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable

# Typed-edge vocabulary (Aeon) — used by SPARQL `edge:KIND` predicates.
# These are the canonical edge kinds actually WRITTEN by writer.py +
# consolidate.py. Note: supersession is captured via CONTRADICTS edges
# (writer writes CONTRADICTS with meta indicating kind), so there is no
# standalone SUPERSEDS constant exported from edges.py.
from cortexm.trace.edges import (
    CAUSAL, REFERS_TO, CONTRADICTS, MERGED_WITH,
    EXTRACTED_FROM, TEMPORALLY_PRECEDED_BY, NEXT,
)
# SPARQL-facing aliases: edge:SUPERSEDES resolves to the actual
# CONTRADICTS replacement edge. This keeps the user-facing vocabulary
# stable even though internally we collapse SUPERSEDE → CONTRADICTS.
SUPERSEDS = CONTRADICTS  # alias for `edge:SUPERSEDES` queries


# ----------------------------------------------------------- parser
def parse_sparql(query: str) -> dict:
    """Parse a SPARQL SELECT query.

    Supports (v2, extended):
        SELECT [DISTINCT] ?var+ WHERE {
            triple-pattern ( ?s ?p ?o . | ?s "literal" ?o . | ... )
            FILTER ( regex(?v, "pat", "i") | ?v = "lit" | ?v != "lit" )
            OPTIONAL { ... }      # left-join semantics
        }
        [ORDER BY ?var [ASC|DESC]]
        [LIMIT N]
        [OFFSET N]

    Typed-edge shortcut: `edge:KIND` is recognized as a predicate
    where KIND is one of the canonical edge kinds (CAUSAL, REFERS_TO,
    SUPERSEDES, CONTRADICTS, MERGED_WITH, EXTRACTED_FROM,
    TEMPORALLY_PRECEDED_BY, NEXT). The triple (?a edge:CAUSAL ?b)
    queries the Trace's edges table instead of the facts table.

    Returns:
        {
          "distinct": bool,
          "select":   ["?s", "?p", "?o"],
          "patterns": [("?s", "?p", "?o"), ...],
          "optionals":[[pat, ...], ...],     # one list per OPTIONAL block
          "filters":  [{"type": "regex"/"equals"/"ne", ...}, ...],
          "order_by": {"var": "?s", "desc": False} | None,
          "limit":    int | None,
          "offset":   int | None,
        }

    Raises ValueError on unparseable input.
    """
    query = query.strip()
    # strip trailing semicolons / whitespace
    while query.endswith(";"):
        query = query[:-1].strip()

    # match: SELECT [DISTINCT] <select-vars> WHERE { <where> }
    # select-vars may be one or more ?var tokens, or *
    sel_grp = ""
    where_clause = ""
    tail = ""
    distinct_grp = ""
    matched = False
    for pat in (
        # canonical form with WHERE keyword
        r"SELECT\s+(DISTINCT\s+)?((?:\?\S+(?:\s+|$))+|\*\s*?)\s*WHERE\s*\{(.*)\}\s*(.*)$",
        # without WHERE keyword (some demos)
        r"SELECT\s+(DISTINCT\s+)?((?:\?\S+(?:\s+|$))+|\*\s*?)\s*\{(.*)\}\s*(.*)$",
    ):
        m = re.match(pat, query, re.IGNORECASE | re.DOTALL)
        if m:
            distinct_grp = m.group(1) or ""
            sel_grp = m.group(2) or ""
            where_clause = m.group(3) or ""
            tail = m.group(4) or ""
            matched = True
            break
    if not matched:
        raise ValueError(
            "query must be 'SELECT [DISTINCT] ?vars WHERE { ... }' "
            "— more complex SPARQL not yet supported in v2")

    # parse SELECT vars (may be multiple ?vars separated by spaces, or *)
    is_distinct = bool(distinct_grp)
    if sel_grp.strip() == "*":
        select_vars: list[str] = ["?s", "?p", "?o"]
    else:
        # find all ?vars in the select clause
        select_vars = re.findall(r"\?\w+", sel_grp)
        if not select_vars:
            raise ValueError("SELECT must list at least one variable")

    # parse tail (ORDER BY / LIMIT / OFFSET — order-insensitive)
    order_by, limit, offset = _parse_tail(tail + " " + where_clause[-0:])
    # NOTE: the WHERE clause may also have a trailing LIMIT etc — we
    # extract the tail BEFORE the where clause's trailing }. The above
    # regex captures tail as everything AFTER the closing }. Good.

    # parse WHERE: triple patterns, FILTER, OPTIONAL
    where_clause = where_clause.strip()
    patterns: list[tuple[str, str, str]] = []
    filters: list[dict] = []
    optionals: list[list[tuple[str, str, str]]] = []

    # tokenize while respecting OPTIONAL { ... } nesting, FILTER(...),
    # and quoted strings. Then merge 'FILTER' + the following
    # parenthesized token so _parse_filter sees the full clause.
    tokens = _merge_filter_tokens(_tokenize_where(where_clause))

    cur_optional: list[tuple[str, str, str]] | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if not tok:
            i += 1
            continue
        upper = tok.upper()
        if upper.startswith("OPTIONAL"):
            # expect '{' next
            i += 1
            if i >= len(tokens) or tokens[i].strip() != "{":
                raise ValueError("OPTIONAL must be followed by '{'")
            # gather until matching '}'
            depth = 1
            inner: list[str] = []
            i += 1
            while i < len(tokens) and depth > 0:
                t = tokens[i].strip()
                if t == "{":
                    depth += 1
                    inner.append(t)
                elif t == "}":
                    depth -= 1
                    if depth > 0:
                        inner.append(t)
                else:
                    inner.append(t)
                i += 1
            # parse inner as a sub-clause (patterns + filters)
            sub_pats, sub_filts = _parse_triples(inner)
            # v2 stores optional patterns separately; filters inside
            # OPTIONAL are also applied within the left-join
            optionals.append(sub_pats)
            filters.extend(sub_filts)  # FILTERs are hoisted — applied later
            cur_optional = None
            continue
        if upper.startswith("FILTER"):
            filt = _parse_filter(tok)
            if filt:
                filters.append(filt)
            i += 1
            continue
        # else: triple-pattern token. Look at groups of 3.
        # Easier: re-tokenize by '.' but keep quoted strings together
        # _tokenize_where already split on whitespace, so triple tokens
        # come as a stream. Group until we have 3 non-'.' tokens.
        # Skip '.' separator.
        if tok == ".":
            i += 1
            continue
        # collect 3 terms for a triple
        terms = [tok]
        j = i + 1
        while j < len(tokens) and len(terms) < 3:
            t = tokens[j].strip()
            if t == ".":
                j += 1
                continue
            if t.upper().startswith("FILTER") or t.upper().startswith(
                    "OPTIONAL") or t == "}":
                break
            terms.append(t)
            j += 1
        if len(terms) == 3:
            patterns.append(tuple(terms))
        i = j

    return {
        "distinct": is_distinct,
        "select": select_vars,
        "patterns": patterns,
        "optionals": optionals,
        "filters": filters,
        "order_by": order_by,
        "limit": limit,
        "offset": offset,
    }


def _parse_tail(tail: str) -> tuple[dict | None, int | None, int | None]:
    """Extract ORDER BY / LIMIT / OFFSET from the post-WHERE tail."""
    order_by = None
    limit = None
    offset = None

    # ORDER BY ?var [ASC|DESC]
    m = re.search(
        r"ORDER\s+BY\s+(\?\w+)(?:\s+(ASC|DESC))?",
        tail, re.IGNORECASE)
    if m:
        order_by = {"var": m.group(1),
                    "desc": (m.group(2) or "").upper() == "DESC"}

    # LIMIT N
    m = re.search(r"LIMIT\s+(\d+)", tail, re.IGNORECASE)
    if m:
        limit = int(m.group(1))

    # OFFSET N
    m = re.search(r"OFFSET\s+(\d+)", tail, re.IGNORECASE)
    if m:
        offset = int(m.group(1))

    return order_by, limit, offset


def _tokenize_where(s: str) -> list[str]:
    """Split WHERE body into tokens, preserving quoted strings + parens."""
    tokens: list[str] = []
    cur = ""
    in_string = False
    quote_char = None
    depth = 0
    for ch in s:
        if in_string:
            cur += ch
            if ch == quote_char:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            cur += ch
            continue
        if ch == "(":
            depth += 1
            cur += ch
            continue
        if ch == ")":
            depth -= 1
            cur += ch
            continue
        if ch == "{":
            if cur.strip():
                tokens.append(cur.strip())
                cur = ""
            tokens.append("{")
            continue
        if ch == "}":
            if cur.strip():
                tokens.append(cur.strip())
                cur = ""
            tokens.append("}")
            continue
        if depth > 0:
            cur += ch
            continue
        if ch.isspace():
            if cur.strip():
                tokens.append(cur.strip())
                cur = ""
            continue
        cur += ch
    if cur.strip():
        tokens.append(cur.strip())
    return tokens


def _parse_triples(tokens: list[str]) -> tuple[list[tuple], list[dict]]:
    """Parse a flat list of triple tokens + FILTERs into (pats, filts)."""
    tokens = _merge_filter_tokens(tokens)
    pats: list[tuple] = []
    filts: list[dict] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if not tok:
            i += 1
            continue
        if tok.upper().startswith("FILTER"):
            f = _parse_filter(tok)
            if f:
                filts.append(f)
            i += 1
            continue
        if tok == ".":
            i += 1
            continue
        terms = [tok]
        j = i + 1
        while j < len(tokens) and len(terms) < 3:
            t = tokens[j].strip()
            if t == ".":
                j += 1
                continue
            if t.upper().startswith("FILTER") or t == "}":
                break
            terms.append(t)
            j += 1
        if len(terms) == 3:
            pats.append(tuple(terms))
        i = j
    return pats, filts


def _parse_filter(tok: str) -> dict | None:
    """Parse one FILTER(...) clause into a filter dict.

    `tok` may be:
      - 'FILTER regex(?v, "pat", "flags")'  (merged by tokenizer)
      - 'FILTER(?v = "value")'
      - 'FILTER(?v != "value")'
      - 'FILTER' alone (in which case caller should merge next token)
    """
    # FILTER regex(?v, "pat", "flags")
    m = re.match(
        r"FILTER\s+regex\s*\(\s*(\?\w+)\s*,\s*[\"'](.+?)[\"']\s*"
        r"(?:,\s*[\"'](\w+)[\"']\s*)?\)",
        tok, re.IGNORECASE | re.DOTALL)
    if m:
        return {"type": "regex", "var": m.group(1),
                "pattern": m.group(2), "flags": m.group(3) or ""}
    # FILTER regex without space: FILTERregex(...) — unlikely but tolerated
    m = re.match(
        r"FILTERregex\s*\(\s*(\?\w+)\s*,\s*[\"'](.+?)[\"']\s*"
        r"(?:,\s*[\"'](\w+)[\"']\s*)?\)",
        tok, re.IGNORECASE | re.DOTALL)
    if m:
        return {"type": "regex", "var": m.group(1),
                "pattern": m.group(2), "flags": m.group(3) or ""}
    # FILTER(?v = "value")
    m = re.match(
        r"FILTER\s*\(\s*(\?\w+)\s*=\s*[\"'](.+?)[\"']\s*\)",
        tok, re.IGNORECASE | re.DOTALL)
    if m:
        return {"type": "equals", "var": m.group(1), "value": m.group(2)}
    # FILTER(?v != "value")
    m = re.match(
        r"FILTER\s*\(\s*(\?\w+)\s*!=\s*[\"'](.+?)[\"']\s*\)",
        tok, re.IGNORECASE | re.DOTALL)
    if m:
        return {"type": "ne", "var": m.group(1), "value": m.group(2)}
    return None


def _merge_filter_tokens(tokens: list[str]) -> list[str]:
    """If 'FILTER' appears as a standalone token, merge it with the
    following parenthesized token so _parse_filter sees the full
    'FILTER regex(...)' or 'FILTER(...)' string.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.upper() == "FILTER" and i + 1 < len(tokens):
            merged = t + " " + tokens[i + 1]
            out.append(merged)
            i += 2
            continue
        out.append(t)
        i += 1
    return out


# ----------------------------------------------------------- matcher
def match_triple(triple: tuple, pattern: tuple,
                 bindings: dict | None = None) -> dict | None:
    """Try to match a triple against a pattern, returning extended
    bindings or None if no match.

    triple:   (subject, relation, value) or (subject, relation, value,
              source_id) from Context-M facts. If a 4-tuple is passed,
              the source_id is stashed under the synthetic `__source_id`
              key (not projectable) so the executor can resolve arena
              text on demand.
    pattern:  ("?s", "?p", "?o") or with literals like
              ("?s", "name", "?o")
    """
    if len(triple) not in (3, 4) or len(pattern) != 3:
        return None
    b = dict(bindings or {})
    # if the triple has a source_id, stash it for the executor
    if len(triple) == 4:
        b["__source_id"] = triple[3]
    for tri_elem, pat_elem in zip(triple[:3], pattern):
        if pat_elem.startswith("?"):
            if pat_elem in b:
                if b[pat_elem] != tri_elem:
                    return None
            else:
                b[pat_elem] = tri_elem
        else:
            # literal: strip quotes
            literal = pat_elem.strip('"\'')
            if literal != tri_elem:
                return None
    return b


def apply_filters(bindings: dict, filters: list[dict],
                 blob_resolver=None) -> bool:
    """Return True if `bindings` satisfies all `filters`.

    If `blob_resolver` is provided and a filter targets a variable
    whose value is the synthetic `__source_id` (a chunk_id pointing
    into the sidecar blob arena), we resolve the source text on
    demand and apply the filter against the resolved text. This makes
    `FILTER regex(?src, "pat")` work when ?src is bound to a chunk_id
    via a special `?_source` projection variable.
    """
    for f in filters:
        var = f["var"]
        if var not in bindings:
            # special case: ?_source / ?source_text refers to the
            # arena-resolved chunk text; we resolve lazily here.
            if var in ("?_source", "?source_text") and blob_resolver:
                src_id = bindings.get("__source_id")
                if not src_id:
                    return False
                try:
                    val = blob_resolver(None, None, src_id) or ""
                except Exception:
                    val = ""
                if f["type"] == "regex":
                    flags = 0
                    if "i" in f.get("flags", ""):
                        flags |= re.IGNORECASE
                    if not re.search(f["pattern"], val, flags):
                        return False
                elif f["type"] == "equals":
                    if val != f["value"]:
                        return False
                elif f["type"] == "ne":
                    if val == f["value"]:
                        return False
                continue
            return False
        val = bindings[var]
        if f["type"] == "regex":
            flags = 0
            if "i" in f.get("flags", ""):
                flags |= re.IGNORECASE
            if not re.search(f["pattern"], val, flags):
                return False
        elif f["type"] == "equals":
            if val != f["value"]:
                return False
        elif f["type"] == "ne":
            if val == f["value"]:
                return False
    return True


# ----------------------------------------------------------- edge triples
# Mapping from SPARQL `edge:KIND` predicates to the actual edge kinds
# stored in the Trace. External graph tools get a stable SPARQL-facing
# vocabulary that maps to Aeon's typed-edge graph internally.
_EDGE_PRED_TO_KIND = {
    "edge:CAUSAL": CAUSAL,
    "edge:REFERS_TO": REFERS_TO,
    "edge:SUPERSEDES": SUPERSEDS,
    "edge:CONTRADICTS": CONTRADICTS,
    "edge:MERGED_WITH": MERGED_WITH,
    "edge:EXTRACTED_FROM": EXTRACTED_FROM,
    "edge:TEMPORALLY_PRECEDED_BY": TEMPORALLY_PRECEDED_BY,
    "edge:NEXT": NEXT,
}


def is_edge_predicate(p: str) -> bool:
    """Is this a typed-edge predicate (edge:KIND)?"""
    return p in _EDGE_PRED_TO_KIND


def edge_triples(mem, user_id: str | None = None) -> list[tuple]:
    """Yield all typed edges as (src, edge:KIND, dst) triples.

    Lets SPARQL queries like `?a edge:CAUSAL ?b` walk the typed-edge
    graph natively. We materialize on demand — the edges table is
    usually 10^4-10^5 rows so this is fast.
    """
    rows = mem.store.conn.execute(
        "SELECT src, kind, dst FROM edges").fetchall()
    out = []
    for src, kind, dst in rows:
        # reverse-map kind to SPARQL-facing predicate
        for sp_pred, k in _EDGE_PRED_TO_KIND.items():
            if k == kind:
                out.append((src, sp_pred, dst))
                break
    return out


# ----------------------------------------------------------- executor
def execute_sparql(query: str, facts: list,
                   user_id: str | None = None,
                   *, edge_triples: list | None = None,
                   blob_resolver=None) -> dict:
    """Execute a parsed SPARQL query against a list of facts.

    facts: list of (subject, relation, value) triples from the facts table
    edge_triples: optional list of (src, edge:KIND, dst) triples from the
                  edges table (for typed-edge queries). If None, edge:
                  predicates won't match anything.
    blob_resolver: optional callable(subject, relation, value) -> str that
                  resolves long object values stored in the sidecar blob
                  arena. If a SPARQL FILTER references the object text
                  and the value is a chunk_id pointing into the arena,
                  the resolver dereferences on demand.

    Returns SPARQL JSON Results format dict.
    """
    parsed = parse_sparql(query)
    select_vars = parsed["select"]
    patterns = parsed["patterns"]
    optionals = parsed["optionals"]
    filters = parsed["filters"]
    order_by = parsed["order_by"]
    limit = parsed["limit"]
    offset = parsed["offset"] or 0

    if not patterns and not optionals:
        # empty WHERE — return all triples
        patterns = [("?s", "?p", "?o")]
        if not select_vars or select_vars == ["*"]:
            select_vars = ["?s", "?p", "?o"]

    # split patterns into fact-patterns vs edge-patterns so we
    # search the right materialized triple list
    fact_pats = [p for p in patterns if not is_edge_predicate(p[1])]
    edge_pats = [p for p in patterns if is_edge_predicate(p[1])]
    edge_pool = edge_triples or []

    # naive nested-loop join: for each pattern, extend the binding set
    bindings_list: list[dict] = [{}]
    for pat in fact_pats:
        new_list = []
        for b in bindings_list:
            for tri in facts:
                nb = match_triple(tri, pat, b)
                if nb is not None:
                    new_list.append(nb)
        bindings_list = new_list
        if not bindings_list:
            break
    # edge patterns join against the edges materialized view
    for pat in edge_pats:
        new_list = []
        for b in bindings_list:
            for tri in edge_pool:
                nb = match_triple(tri, pat, b)
                if nb is not None:
                    new_list.append(nb)
        bindings_list = new_list
        if not bindings_list:
            break

    # OPTIONAL: left-join each optional pattern group
    for opt_pats in optionals:
        new_list = []
        for b in bindings_list:
            extended = [b]
            for op in opt_pats:
                pool = edge_pool if is_edge_predicate(op[1]) else facts
                next_ext = []
                for bb in extended:
                    for tri in pool:
                        nb = match_triple(tri, op, bb)
                        if nb is not None:
                            next_ext.append(nb)
                extended = next_ext if next_ext else [b]  # left-join: keep b
            # add all extended bindings (or original if no match)
            new_list.extend(extended)
        bindings_list = new_list

    # apply filters (pass blob_resolver for ?_source / ?source_text)
    if filters:
        bindings_list = [b for b in bindings_list
                         if apply_filters(b, filters, blob_resolver)]

    # blob resolution: if the SELECT projects ?_source / ?source_text
    # AND a blob_resolver was provided, resolve the chunk text from
    # the sidecar arena for each binding (using the stashed __source_id).
    # This makes the SPARQL surface actually useful for arena-stored
    # content — `SELECT ?s ?source_text WHERE { ?s ?p ?o }` returns
    # the source text of the chunk each fact was extracted from.
    if blob_resolver and select_vars:
        source_vars = [v for v in select_vars
                       if v in ("?_source", "?source_text")]
        if source_vars:
            for b in bindings_list:
                src_id = b.get("__source_id")
                if src_id:
                    try:
                        txt = blob_resolver(None, None, src_id) or ""
                    except Exception:
                        txt = ""
                    for v in source_vars:
                        b[v] = txt

    # ORDER BY
    if order_by:
        var = order_by["var"]
        desc = order_by["desc"]
        bindings_list.sort(
            key=lambda b: (b.get(var) or ""),
            reverse=desc)

    # DISTINCT
    if parsed["distinct"]:
        seen: set = set()
        deduped = []
        for b in bindings_list:
            key = tuple((v, b.get(v)) for v in select_vars)
            if key not in seen:
                seen.add(key)
                deduped.append(b)
        bindings_list = deduped

    # OFFSET
    if offset:
        bindings_list = bindings_list[offset:]

    # LIMIT
    if limit is not None:
        bindings_list = bindings_list[:limit]

    # project to SELECT vars
    rows = []
    for b in bindings_list:
        row = {v: b.get(v, "") for v in select_vars}
        rows.append(row)

    return {
        "head": {"vars": select_vars},
        "results": {"bindings": rows},
        "n_results": len(rows),
    }


# Maximum query string length (bytes) — protects against pathological
# SPARQL queries that would blow up the regex parser or nested-loop
# joiner. SPARQL queries in practice are <1KB; 64KB is generous.
MAX_QUERY_BYTES = 64 * 1024

# Maximum body size for SPARQL POST (smaller than REST MAX_BODY because
# SPARQL queries are text-only, no payloads to ingest)
MAX_BODY_BYTES = 256 * 1024  # 256 KiB


# ----------------------------------------------------------- HTTP server
def make_handler(mem, user_id: str | None = None,
                 *, auth_keys=None, require_auth: bool = False):
    """Build a BaseHTTPRequestHandler bound to a Memory instance.

    Auth: when `require_auth=True`, the handler validates a Bearer API
    key against `auth_keys` (an APIKeyStore) and checks the
    `sparql.query` permission. This is OFF by default for localhost
    loopback use; the REST server turns it ON when co-hosting the
    SPARQL endpoint so external graph tools go through the same RBAC
    as the REST API.
    """

    class SparqlHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str,
                  content_type: str = "application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                              "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                              "Content-Type, Authorization")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_OPTIONS(self):
            # CORS preflight — return 204 with headers
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                              "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                              "Content-Type, Authorization")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _auth(self) -> tuple[dict, str] | None:
            """Validate the Bearer token; return (meta, actor) or None.

            On None, the caller has already sent a 401/403 response.
            """
            if not require_auth or auth_keys is None:
                # open mode — used for loopback standalone SPARQL.
                # CAUTION: only safe behind a firewall or 127.0.0.1.
                return ({"role": "admin", "label": "open-sparql"},
                        "open-sparql")
            hdr = self.headers.get("Authorization") or ""
            if not hdr.startswith("Bearer "):
                self._send(401, json.dumps({
                    "error": "SPARQL endpoint requires a Bearer API key "
                              "(use the same key as the REST API)"}))
                return None
            key = hdr[7:].strip()
            meta = auth_keys.verify(key)
            if meta is None:
                self._send(401, json.dumps({"error": "invalid or revoked key"}))
                return None
            # check sparql.query permission
            from cortexm.security.rbac import authorize, RBACError
            try:
                authorize(meta, "sparql.query")
            except RBACError as e:
                self._send(403, json.dumps({"error": str(e)}))
                return None
            actor = meta.get("label") or meta.get("id", "key")
            return (meta, actor)

        def do_GET(self):
            parsed_url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed_url.query)
            # URL length guard (414 URI Too Long)
            if len(parsed_url.query) > MAX_QUERY_BYTES:
                self._send(414, json.dumps({
                    "error": f"query string exceeds {MAX_QUERY_BYTES} bytes"}))
                return
            auth = self._auth()
            if auth is None:
                return
            query = params.get("query", [""])[0]
            self._handle_query(query, auth)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_BYTES:
                self._send(413, json.dumps({
                    "error": f"body exceeds {MAX_BODY_BYTES} bytes"}))
                return
            body = self.rfile.read(length).decode("utf-8") if length else ""
            content_type = self.headers.get("Content-Type", "")
            if "x-www-form-urlencoded" in content_type:
                params = urllib.parse.parse_qs(body)
                query = params.get("query", [""])[0]
            else:
                query = body
            auth = self._auth()
            if auth is None:
                return
            self._handle_query(query, auth)

        def _handle_query(self, query: str, auth: tuple[dict, str]):
            meta, actor = auth
            if not query:
                self._send(400, json.dumps({
                    "error": "missing 'query' parameter — send "
                              "'?query=SELECT ...' or POST a SPARQL query"}))
                return
            if len(query) > MAX_QUERY_BYTES:
                self._send(413, json.dumps({
                    "error": f"query exceeds {MAX_QUERY_BYTES} bytes"}))
                return
            try:
                # pull facts + edges (scope to user_id if SPARQL endpoint
                # was started with --sparql-user-id; otherwise all users)
                fact_objs = mem.store.query_facts(active=True, user_id=user_id)
                # build (s, p, o, source_id) 4-tuples so the blob resolver
                # can dereference source text via fact.source_id — which is
                # the actual chunk_id (NOT fact.value, which holds the
                # literal). This fixes a v2 bug where the resolver never
                # fired because chunk_ids never appeared in `value`.
                facts = [(f.subject, f.relation, f.value, f.source_id)
                         for f in fact_objs]
                edges = edge_triples(mem, user_id=user_id)
                # build blob resolver if arena is enabled
                blob_resolver = None
                arena = getattr(mem, "blob_arena", None)
                if arena is not None:
                    def _resolve(_s, _r, source_id):
                        if not source_id:
                            return ""
                        from cortexm.trace.blob_arena import get_chunk_text
                        return get_chunk_text(mem.store, arena, source_id)
                    blob_resolver = _resolve
                result = execute_sparql(query, facts, user_id,
                                         edge_triples=edges,
                                         blob_resolver=blob_resolver)
                # audit (if auth is on AND a memory audit_log exists)
                if require_auth and getattr(mem, "audit_log", None):
                    mem.audit_log.log(
                        "sparql.query", actor=actor,
                        role=meta.get("role"),
                        meta={"n_results": result.get("n_results", 0),
                               "query_head": query[:80]})
                self._send(200, json.dumps(result, indent=2, default=str))
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"server: {e}"}))

        def log_message(self, fmt, *args):
            pass  # quiet

    return SparqlHandler


class SparqlServer:
    """A minimal SPARQL HTTP endpoint backed by a Memory instance.

    The Memory's reader is configured with the RDFDecoder so retrieval
    produces RDF triples instead of an LLM prompt. This server exposes
    those triples via SPARQL — a fully non-LLM retrieval path.

    Threaded: uses ThreadingHTTPServer so concurrent queries don't
    block each other. Each thread reads from the shared Memory (which
    is itself guarded by an RLock at the store level).

    Auth: when `require_auth=True` (the default when bound to non-
    loopback interfaces), the server validates a Bearer API key
    against the Memory's APIKeyStore and checks `sparql.query`
    permission. When bound to 127.0.0.1 (default), auth is OFF for
    local-dev convenience — but `--sparql-host 0.0.0.0` automatically
    enables auth to prevent an unauthenticated fact dump.
    """
    def __init__(self, mem, host: str = "127.0.0.1", port: int = 8910,
                 user_id: str | None = None,
                 require_auth: bool | None = None) -> None:
        self.mem = mem
        self.host = host
        self.port = port
        self.user_id = user_id
        # auth is auto-enabled whenever binding to non-loopback
        if require_auth is None:
            require_auth = host not in ("127.0.0.1", "localhost", "::1")
        self.require_auth = require_auth
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        auth_keys = self.mem.keys if self.require_auth else None
        handler_cls = make_handler(self.mem, user_id=self.user_id,
                                     auth_keys=auth_keys,
                                     require_auth=self.require_auth)
        self._server = ThreadingHTTPServer((self.host, self.port),
                                            handler_cls)
        self._server.daemon_threads = True
        print(f"[sparql] listening on http://{self.host}:{self.port}/"
              f"  (SPARQL 1.1 Protocol; thread-per-request)")
        auth_msg = ("on (Bearer + sparql.query)" if self.require_auth
                    else "OFF (loopback only)")
        print(f"[sparql]   auth: {auth_msg}")
        print(f"[sparql]   user_id: {self.user_id or '(all users)'}")
        print(f"[sparql]   try: curl 'http://{self.host}:{self.port}/"
              f"?query=SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20"
              f"%3Fp%20%3Fo%20%7D%20LIMIT%2010'")

    def start_background(self) -> None:
        """Start the server in a daemon thread (non-blocking).

        Used by `cortexm serve-rest --sparql-port N` to share one Memory
        instance across both the REST API and the SPARQL endpoint.
        """
        self.start()
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name=f"sparql-{self.port}")
        self._thread.start()

    def serve_forever(self) -> None:
        if self._server is None:
            self.start()
        assert self._server is not None
        self._server.serve_forever()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="contextm-sparql",
                                  description="Context-M SPARQL endpoint")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8910)
    ap.add_argument("--db", default=None,
                    help="path to the Context-M DB (default: in-memory)")
    ap.add_argument("--user-id", default=None,
                    help="scope queries to a single user")
    args = ap.parse_args()

    from cortexm.api.memory import Memory
    from cortexm.config import Config
    cfg = Config.from_env()
    if args.db:
        cfg.db_path = args.db
    mem = Memory(cfg)
    server = SparqlServer(mem, host=args.host, port=args.port,
                           user_id=args.user_id)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[sparql] shutting down")
    finally:
        server.stop()
        mem.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
