"""InjecMEM defense — memory-injection pattern detection (Section 0.3).

A single crafted interaction must not be able to poison the Trace.
High-risk patterns quarantine the offending fact (stored, hash-chained,
but never active nor retrievable into prompt context). Medium-risk
patterns flag provenance for downstream audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HIGH_RISK = [
    ("ignore_instructions", re.compile(
        r"ignore\s+(?:all\s+|any\s+|the\s+|those\s+)?(?:previous|prior|above|earlier|preceding|past)\s+"
        r"(?:instructions?|rules?|prompts?|directives?|messages?)", re.I)),
    ("disregard_context", re.compile(
        r"disregard\s+(?:the\s+|all\s+|any\s+)?(?:above|previous|prior|earlier|context)", re.I)),
    ("system_prompt_probe", re.compile(
        r"(?:reveal|show|print|repeat|output|expose|leak)\s+(?:your\s+|the\s+|this\s+)?"
        r"(?:system|developer|initial)\s+(?:prompt|instructions?|message)", re.I)),
    ("identity_override", re.compile(
        r"you\s+(?:are\s+now|must\s+now|will\s+now|from\s+now\s+on)\s+(?:act\s+as|become|behave\s+as|forget)", re.I)),
    ("jailbreak", re.compile(
        r"\b(?:jailbreak|DAN\s+mode|developer\s+mode|do\s+anything\s+now)\b", re.I)),
    ("exfiltration", re.compile(
        r"\b(?:exfiltrate|upload|send)\s+(?:the\s+|all\s+|every\s+)?(?:memory|memories|database|records?|secrets?|api\s+keys?|credentials?|passwords?|tokens?)", re.I)),
    ("credential_capture", re.compile(
        r"\b(?:api\s+key|secret\s+key|access\s+token|password)\s*(?:is|:|=)\s*\S+", re.I)),
]

MEDIUM_RISK = [
    ("always_respond", re.compile(
        r"always\s+(?:respond|reply|answer|say|output)\s+(?:with|in)\b", re.I)),
    ("must_remember", re.compile(
        r"remember\s+that\s+you\s+(?:must|always|never|are\s+no\s+longer)", re.I)),
    ("memory_overwrite", re.compile(
        r"(?:overwrite|replace|wipe|erase|delete)\s+(?:all\s+|the\s+|your\s+)?memory", re.I)),
    ("role_injection", re.compile(
        r"\b(?:from\s+now\s+on|for\s+all\s+future\s+(?:sessions?|turns?|conversations?))\b[^.]{0,80}?"
        r"(?:you\s+are|act\s+as|always)", re.I)),
]

BENIGN_EXCEPTIONS = re.compile(
    r"\b(?:never\s+ignore|don'?t\s+ignore|do\s+not\s+ignore)\b", re.I)


@dataclass
class InjectionVerdict:
    risk: str                  # "none" | "medium" | "high"
    rules: list[str]
    quarantined: bool
    note: str = ""


def scan(text: str, quarantine_high: bool = True) -> InjectionVerdict:
    hits_high = [name for name, rx in HIGH_RISK if rx.search(text)]
    hits_med = [name for name, rx in MEDIUM_RISK if rx.search(text)]
    # negations like "never ignore previous instructions" (user quoting policy)
    if BENIGN_EXCEPTIONS.search(text):
        hits_high = [r for r in hits_high if r != "ignore_instructions"]
    if hits_high:
        return InjectionVerdict(
            risk="high", rules=hits_high, quarantined=quarantine_high,
            note="InjecMEM high-risk pattern; fact quarantined (stored for audit, never active).")
    if hits_med:
        return InjectionVerdict(
            risk="medium", rules=hits_med, quarantined=False,
            note="InjecMEM medium-risk pattern; committed with audit flag.")
    return InjectionVerdict(risk="none", rules=[], quarantined=False)


# ---------------------------------------------------------------------------
# Second-order defense (MINJA, arXiv:2503.03704).
#
# MINJA showed that an attacker does NOT need write access to the memory
# bank: craft a query whose *retrieved* answer gets written back by the
# agent itself, and the poison enters the Trace through the front door.
# Countermeasure: quarantined source text is itself a tainted corpus — any
# subsequent ingest that substantially overlaps it (even when punctuation
# / ordering edits defeat the regex patterns) is quarantined too
# ("contagion guard"), so poison cannot launder itself back into active
# memory through re-ingestion loops. Deep paraphrase laundering is a
# documented limitation (see docs/SECURITY.md).
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _token_set(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text.lower()))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text) if s and s.strip()]


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def contagion_scan(text: str, quarantined_texts, threshold: float = 0.50,
                   quarantine: bool = True) -> InjectionVerdict | None:
    """Return a HIGH-risk verdict if `text` substantially overlaps any
    already-quarantined source text (MINJA re-ingestion loop). None if clean.

    Sentence-to-sentence token Jaccard plus a verbatim-substring shortcut,
    so punctuation/ordering edits that defeat the regex patterns are still
    caught. Pure set arithmetic — no LLM calls, write path stays μ=0.
    """
    new_sents = _sentences(text)
    if not new_sents or not quarantined_texts:
        return None
    tainted_sents: list[frozenset[str]] = []
    tainted_raw: list[str] = []
    for qt in quarantined_texts:
        for s in _sentences(qt):
            toks = _token_set(s)
            if len(toks) >= 4:
                tainted_sents.append(toks)
                tainted_raw.append(s)
    if not tainted_sents:
        return None
    best_sim = 0.0
    for ns in new_sents:
        nt = _token_set(ns)
        if not nt:
            continue
        low = ns.lower()
        # verbatim-quote shortcut (the classic MINJA write-back)
        for raw in tainted_raw:
            if len(raw) >= 25 and raw.lower() in low:
                return InjectionVerdict(
                    risk="high", rules=["minja_contagion"], quarantined=quarantine,
                    note=("Second-order injection (MINJA): verbatim quote of "
                          "quarantined source; quarantined by contagion."))
        for tt in tainted_sents:
            best_sim = max(best_sim, jaccard(nt, tt))
    if best_sim >= threshold:
        return InjectionVerdict(
            risk="high", rules=["minja_contagion"], quarantined=quarantine,
            note=(f"Second-order injection (MINJA): {best_sim:.0%} token "
                  "overlap with quarantined source; quarantined by contagion."))
    return None
