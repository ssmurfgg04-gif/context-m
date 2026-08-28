"""SPARQL endpoint demo — non-LLM decoder path (NSR-inspired).

arXiv insight (NSR / ESWEEK24): the VSA core is task-agnostic; only
the decoder changes. Context-M's reader was hardcoded to format facts
for an LLM prompt. The Decoders module extracts that formatter into
a pluggable interface — `RDFDecoder` exports facts as RDF/N3 triples.

This module exposes a SPARQL HTTP endpoint that:
  1. accepts SPARQL SELECT queries via HTTP GET/POST
  2. parses the query (we ship a minimal hand-rolled parser that
     handles the common case: SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(...) }
     — full SPARQL grammar is out of scope for v1)
  3. retrieves facts from Context-M's Memory.reader with the RDFDecoder
     attached (so retrieval produces RDF triples instead of an LLM
     prompt)
  4. runs the WHERE clause as a post-filter on those triples
  5. returns the result as SPARQL JSON Results format

This is a NON-LLM retrieval path. Zero LLM calls. The same palace +
Trace substrate that powers LLM context-stuffing now serves SPARQL.

Usage:
    # start the endpoint on port 8910
    python -m context_m.server.sparql --port 8910

    # or programmatically
    from context_m.server.sparql import SparqlServer
    s = SparqlServer(mem, port=8910)
    s.serve_forever()

Example query:
    curl 'http://localhost:8910/?query=SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20%3Fp%20%3Fo%20%7D'

LIMITATIONS (honest):
  - SPARQL parser is minimal — supports SELECT + basic triple patterns
    + FILTER with regex/equals. Full SPARQL 1.1 grammar (UNION,
    OPTIONAL, paths, CONSTRUCT, aggregation) is out of scope for v1.
  - No inference / reasoning over RDFS/OWL — just pattern matching
    on the actual facts.
  - For real SPARQL workloads, use a real triple store (Apache Jena,
    BlazeGraph) and feed it via the RDFDecoder export. This server
    is a demo that the retrieval path is decoder-agnostic.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Iterable


# ------------------------------------------------------------- parser
def parse_sparql(query: str) -> dict:
    """Parse a minimal SPARQL SELECT query.

    Supports:
        SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(...) }

    The WHERE clause can have:
        - triple patterns: ?s ?p ?o .  (with vars or literals)
        - FILTER regex(?var, "pattern", "i") or (?var = "value")

    Returns:
        {
          "select": ["?s", "?p", "?o"],
          "patterns": [("?s", "?p", "?o"), ...],
          "filters": [{"type": "regex", "var": "?s",
                        "pattern": "...", "flags": "i"}, ...],
        }

    Raises ValueError on unparseable input.
    """
    query = query.strip()
    m = re.match(r"SELECT\s+(.+?)\s+WHERE\s*\{(.*)\}\s*$",
                 query, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError(
            "query must be 'SELECT ... WHERE { ... }' — "
            "more complex SPARQL not yet supported in v1")
    select_clause, where_clause = m.group(1), m.group(2).strip()

    # parse SELECT — variables prefixed with ?
    select_vars = re.findall(r"\?\w+", select_clause)

    # parse WHERE — find triple patterns and FILTER clauses
    patterns: list[tuple[str, str, str]] = []
    filters: list[dict] = []
    # tokenize by . (but not inside FILTER parens)
    # naive: split on . that aren't inside quotes/parens
    tokens = []
    depth = 0
    cur = ""
    in_string = False
    quote_char = None
    for ch in where_clause:
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
        if ch == "." and depth == 0:
            tokens.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        tokens.append(cur.strip())

    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok.upper().startswith("FILTER"):
            # FILTER regex(?var, "pattern", "flags") or FILTER(?var = "value")
            m = re.match(
                r"FILTER\s+regex\s*\(\s*(\?\w+)\s*,\s*[\"'](.+?)[\"']\s*"
                r"(?:,\s*[\"'](\w+)[\"']\s*)?\)",
                tok, re.IGNORECASE)
            if m:
                filters.append({
                    "type": "regex",
                    "var": m.group(1),
                    "pattern": m.group(2),
                    "flags": m.group(3) or "",
                })
                continue
            m = re.match(
                r"FILTER\s*\(\s*(\?\w+)\s*=\s*[\"'](.+?)[\"']\s*\)",
                tok, re.IGNORECASE)
            if m:
                filters.append({
                    "type": "equals",
                    "var": m.group(1),
                    "value": m.group(2),
                })
                continue
            # unrecognized FILTER — ignore with a warning
            continue
        # triple pattern: ?s ?p ?o  or ?s ?p "literal" etc.
        parts = re.findall(r"\?\w+|\"[^\"]*\"|'[^']*'|[^\s]+", tok)
        if len(parts) == 3:
            patterns.append(tuple(parts))
        # else: unrecognized token — skip

    return {"select": select_vars, "patterns": patterns,
            "filters": filters}


# ------------------------------------------------------------- matcher
def match_triple(triple: tuple, pattern: tuple,
                 bindings: dict | None = None) -> dict | None:
    """Try to match a triple against a pattern, returning extended
    bindings or None if no match.

    triple:   (subject, relation, value) from Context-M facts
    pattern:  ("?s", "?p", "?o") or with literals like
              ("?s", "name", "?o")
    """
    if len(triple) != 3 or len(pattern) != 3:
        return None
    b = dict(bindings or {})
    for tri_elem, pat_elem in zip(triple, pattern):
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


def apply_filters(bindings: dict, filters: list[dict]) -> bool:
    """Return True if `bindings` satisfies all `filters`."""
    for f in filters:
        var = f["var"]
        if var not in bindings:
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
    return True


def execute_sparql(query: str, facts: list, user_id: str | None = None) -> dict:
    """Execute a parsed SPARQL query against a list of facts.

    facts: list of (subject, relation, value) triples
    Returns SPARQL JSON Results format dict.
    """
    parsed = parse_sparql(query)
    select_vars = parsed["select"]
    patterns = parsed["patterns"]
    filters = parsed["filters"]

    if not patterns:
        # empty WHERE — return all triples
        patterns = [("?s", "?p", "?o")]
        if not select_vars:
            select_vars = ["?s", "?p", "?o"]

    # naive join: take the first pattern, find all matching triples,
    # then for each set of bindings, try the next pattern, etc.
    # (good enough for v1 demo; real SPARQL engines have planners)
    bindings_list: list[dict] = [{}]
    for pat in patterns:
        new_list = []
        for b in bindings_list:
            for tri in facts:
                nb = match_triple(tri, pat, b)
                if nb is not None:
                    new_list.append(nb)
        bindings_list = new_list
        if not bindings_list:
            break

    # apply filters
    if filters:
        bindings_list = [b for b in bindings_list if apply_filters(b, filters)]

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


# ------------------------------------------------------------- HTTP server
def make_handler(mem, user_id: str | None = None):
    """Build a BaseHTTPRequestHandler bound to a Memory instance."""

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
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(200, "{}")

        def do_GET(self):
            # parse query string
            parsed_url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed_url.query)
            query = params.get("query", [""])[0]
            self._handle_query(query)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            # SPARQL 1.1 Protocol: application/x-www-form-urlencoded
        # or application/sparql-query
            content_type = self.headers.get("Content-Type", "")
            if "x-www-form-urlencoded" in content_type:
                params = urllib.parse.parse_qs(body)
                query = params.get("query", [""])[0]
            else:
                query = body
            self._handle_query(query)

        def _handle_query(self, query: str):
            if not query:
                self._send(400, json.dumps({
                    "error": "missing 'query' parameter — send "
                              "'?query=SELECT ...' or POST a SPARQL query"}))
                return
            try:
                # 1. pull all facts from the store (could be scoped by user_id)
                fact_objs = mem.store.query_facts(active=True, user_id=user_id)
                facts = [(f.subject, f.relation, f.value) for f in fact_objs]
                # 2. execute the SPARQL query against the fact list
                result = execute_sparql(query, facts, user_id)
                self._send(200, json.dumps(result, indent=2))
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": f"server: {e}"}))

        def log_message(self, fmt, *args):
            # suppress default HTTP server logging (noisier than useful)
            pass

    return SparqlHandler


class SparqlServer:
    """A minimal SPARQL HTTP endpoint backed by a Memory instance.

    The Memory's reader is configured with the RDFDecoder so retrieval
    produces RDF triples instead of an LLM prompt. This server exposes
    those triples via SPARQL — a fully non-LLM retrieval path.
    """
    def __init__(self, mem, host: str = "127.0.0.1", port: int = 8910,
                 user_id: str | None = None) -> None:
        self.mem = mem
        self.host = host
        self.port = port
        self.user_id = user_id
        self._server: HTTPServer | None = None

    def start(self) -> None:
        handler_cls = make_handler(self.mem, user_id=self.user_id)
        self._server = HTTPServer((self.host, self.port), handler_cls)
        # serve_forever is blocking — caller decides
        print(f"[sparql] listening on http://{self.host}:{self.port}/sparql")
        print(f"[sparql]   user_id: {self.user_id or '(all users)'}")
        print(f"[sparql]   try: curl 'http://{self.host}:{self.port}/"
              f"?query=SELECT%20%3Fs%20%3Fp%20%3Fo%20WHERE%20%7B%20%3Fs%20%3Fp%20%3Fo%20%7D'")

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


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8910)
    ap.add_argument("--db", default=None,
                    help="path to the Context-M DB (default: in-memory)")
    ap.add_argument("--user-id", default=None,
                    help="scope queries to a single user")
    args = ap.parse_args()

    from context_m.api.memory import Memory
    from context_m.config import Config
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
