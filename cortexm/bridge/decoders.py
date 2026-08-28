"""Swappable retrieval decoders — NSR-inspired (ESWEEK24).

arXiv insight: the VSA core is task-agnostic; only the decoder
changes. Context-M's reader was hardcoded to format facts for an LLM
prompt (the `_context_block` method). This module extracts that
formatter into a pluggable decoder interface so the SAME palace + Trace
can serve multiple output formats:

    - LLMPromptDecoder   — current behavior, formats facts as a
                            "[Memory — Known facts]" block for LLM
                            context-stuffing
    - RDFDecoder         — export facts as RDF/N3 triples for external
                            graph DBs (SPARQL queryable)
    - DatalogDecoder     — emit facts as Datalog clauses for the
                            contradiction engine to consume
    - JSONDecoder        — return facts as plain JSON for API responses

All decoders take the same inputs (query, facts, scores, notes,
store) and return a string. The reader picks one via
`reader.with_decoder(decoder)` or by passing `decoder=` to .search().
The default remains LLMPromptDecoder so existing callers don't break.

Why this matters: the palace / Trace substrate is now reusable for
non-LLM workloads (RDF export, contradiction proofs, audit JSON)
without duplicating the retrieval pipeline.
"""
from __future__ import annotations

import json
from typing import Iterable, Protocol


class Decoder(Protocol):
    """Pluggable retrieval-output decoder.

    A decoder takes the post-fusion fact list + scores + procedural
    notes and renders them in a domain-specific format. The reader
    pipeline (intent plan → palace search → fusion → notes) is
    identical across decoders — only the output shape changes.
    """
    name: str

    def render(self, *, query: str, intent: str, facts: list,
               scores: dict, notes: list[str] | None,
               store=None) -> str: ...


# ---------- LLM prompt decoder (current behavior) ----------------------

class LLMPromptDecoder:
    """Format facts as a context block for LLM context-stuffing.

    This is the original reader._context_block output. Kept verbatim
    so existing prompts continue to work.
    """
    name = "llm_prompt"

    def render(self, *, query, intent, facts, scores, notes, store=None) -> str:
        lines = ["[Memory — Known facts]"]
        if not facts and not notes:
            lines.append("(no verified facts found for this query)")
        for f in facts:
            chunk = store.get_chunk(f.source_id) if (
                store and f.source_id) else None
            snippet = ""
            if chunk:
                snippet = chunk["text"].replace("\n", " ")[:80]
                if len(chunk["text"]) > 80:
                    snippet += "..."
            lines.append(
                f"- {f.display()} [valid {f.valid_window()}; "
                f"learned {f.tx_from[:10]}; conf {f.confidence:.2f}; "
                f"id {f.id[:8]}; src #{f.source_hash[:8]}; \"{snippet}\"]")
        for n in (notes or []):
            lines.append(f"- {n}")
        return "\n".join(lines)


# ---------- RDF / N3 decoder ------------------------------------------

class RDFDecoder:
    """Export facts as RDF/N3 triples.

    Format: <subject> <relation> <value> .
    Subjects/values are URI-escaped; string literals quoted. Useful
    for piping Context-M's verified facts into an external graph DB
    (e.g. Apache Jena, BlazeGraph) for SPARQL queries.

    NOTE: namespaces are bare (no @prefix). Callers can post-process
    to add prefix declarations if desired.
    """
    name = "rdf"

    def render(self, *, query, intent, facts, scores, notes, store=None) -> str:
        out: list[str] = []
        for f in facts:
            out.append(f"{self._term(f.subject)} "
                       f"{self._term(f.relation)} "
                       f"{self._term(f.value)} .")
        if notes:
            for n in notes:
                out.append(f"# note: {n}")
        return "\n".join(out)

    @staticmethod
    def _term(s: str) -> str:
        # bare URI if alnum+CWD, else quoted literal
        if not s:
            return '""'
        # treat CamelCase / dotted names as URIs in the local : namespace
        if all(c.isalnum() or c in "._-:/" for c in s) and not s[0].isdigit():
            return f":{s}"
        # quoted literal with N3 escapes
        return '"' + s.replace('"', '\\"') + '"'


# ---------- Datalog decoder -------------------------------------------

class DatalogDecoder:
    """Emit facts as Datalog clauses for the contradiction engine.

    Format: relation(subject, value).
    Used by the contradiction / lifecycle engines to reason over
    verified facts as logic predicates. The current Trace.rules
    engine already speaks this dialect — this decoder exposes the
    same format for external callers.
    """
    name = "datalog"

    def render(self, *, query, intent, facts, scores, notes, store=None) -> str:
        out: list[str] = []
        for f in facts:
            out.append(f"{f.relation}({self._atom(f.subject)}, "
                       f"{self._atom(f.value)}).")
        if notes:
            for n in notes:
                out.append(f"% note: {n}")
        return "\n".join(out)

    @staticmethod
    def _atom(s: str) -> str:
        # Datalog atom — lowercase, spaces → underscores
        if not s:
            return "_"
        a = s.lower().replace(" ", "_").replace("-", "_")
        # quote if starts with digit or contains non-atom chars
        if a[0].isdigit() or not all(c.isalnum() or c == "_" for c in a):
            return f'"{s}"'
        return a


# ---------- JSON decoder ----------------------------------------------

class JSONDecoder:
    """Return facts as a JSON array of triples.

    Each entry: {subject, relation, value, valid_from, valid_to,
    confidence, id, score}. Used by REST API responses and by
    audit / federation tools that consume structured facts.
    """
    name = "json"

    def render(self, *, query, intent, facts, scores, notes, store=None) -> str:
        out: list[dict] = []
        for f in facts:
            out.append({
                "subject": f.subject,
                "relation": f.relation,
                "value": f.value,
                "valid_from": f.valid_from,
                "valid_to": f.valid_to,
                "tx_from": f.tx_from,
                "confidence": round(f.confidence, 4),
                "id": f.id,
                "score": round(scores.get(f.id, 0.0), 4) if scores else None,
            })
        if notes:
            out.append({"_notes": notes})
        return json.dumps(out, default=str, indent=2)


# ---------- Registry --------------------------------------------------

DECODERS: dict[str, type] = {
    "llm_prompt": LLMPromptDecoder,
    "rdf": RDFDecoder,
    "datalog": DatalogDecoder,
    "json": JSONDecoder,
}


def get_decoder(name: str = "llm_prompt") -> "Decoder":
    """Look up a decoder by name; raises ValueError for unknown names."""
    cls = DECODERS.get(name)
    if cls is None:
        raise ValueError(
            f"unknown decoder '{name}' — known: {list(DECODERS)}")
    return cls()


__all__ = [
    "Decoder", "LLMPromptDecoder", "RDFDecoder", "DatalogDecoder",
    "JSONDecoder", "DECODERS", "get_decoder",
]
