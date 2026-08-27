"""Process-wide counters backing the μ=0 (zero-LLM-ingest) protocol.

Every code path that could invoke a language model must route through
``call_llm``. The default implementation raises: the deterministic core
physically cannot make an LLM call. Optional async enrichment (never used
during benchmarks) increments the counter so audits can prove honesty.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_LLM_CALLS = 0
_INGESTED_TOKENS = 0
_INGESTED_MESSAGES = 0
_EXTRACTED_FACTS = 0
_RETRIEVALS = 0


def llm_calls() -> int:
    return _LLM_CALLS


def bump_llm_call() -> None:
    global _LLM_CALLS
    with _LOCK:
        _LLM_CALLS += 1


def bump_ingest(tokens: int = 0, messages: int = 0, facts: int = 0) -> None:
    global _INGESTED_TOKENS, _INGESTED_MESSAGES, _EXTRACTED_FACTS
    with _LOCK:
        _INGESTED_TOKENS += tokens
        _INGESTED_MESSAGES += messages
        _EXTRACTED_FACTS += facts


def bump_retrieval() -> None:
    global _RETRIEVALS
    with _LOCK:
        _RETRIEVALS += 1


def counters() -> dict:
    return {
        "llm_calls": _LLM_CALLS,                # must be 0 under μ=0 protocol
        "ingested_tokens": _INGESTED_TOKENS,
        "ingested_messages": _INGESTED_MESSAGES,
        "extracted_facts": _EXTRACTED_FACTS,
        "retrievals": _RETRIEVALS,
    }


def reset_counters() -> None:
    global _LLM_CALLS, _INGESTED_TOKENS, _INGESTED_MESSAGES, _EXTRACTED_FACTS, _RETRIEVALS
    with _LOCK:
        _LLM_CALLS = 0
        _INGESTED_TOKENS = 0
        _INGESTED_MESSAGES = 0
        _EXTRACTED_FACTS = 0
        _RETRIEVALS = 0
