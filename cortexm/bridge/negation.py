"""Negation detection + indexing as metadata (not as a positive fact).

WHY THIS MODULE EXISTS
-----------------------
The v0.5.x extractor turns "I don't eat meat" into a positive fact
``(user, eats, meat)`` if the pattern fires before the negation is
checked. The reader then answers "Do they eat meat?" with "Yes" —
hallucinating from the very text that denied it.

Google (pre-BERT) indexes negation as a SEPARATE signal: a document
saying "I don't eat meat" is indexed with ``+meat`` (the term is
present) and ``-preference`` (a negation marker attached). When the
query asks "do they eat meat?", the planner sees the negation in the
source document and downranks or flags it.

This module ports the Google approach to μ=0 Python:
  * ``NEGATION_MARKERS`` — list of negation phrases
  * ``detect_negation(text)`` returns ``[(sentence, marker, span)]``
  * ``extract_with_negation(text)`` returns ``{"facts_text": str,
    "negations": [...]}`` — splits the text into negated + non-negated
    sentences so the structured extractor processes only the non-
    negated ones, and the negated ones go into a separate
    ``negation_records`` table.

ARCHITECTURE
-------------
Negation is detected at SENTENCE level (not token level). A negated
sentence is one that contains any ``NEGATION_MARKER``. The detector
also identifies the IMPLIED SUBJECT (what's being negated) so the
reader can match negations against positive queries.

The reader's path:
  1. Search positive facts normally.
  2. If positive facts found AND no negations overlap the query →
     return the positive facts.
  3. If negations overlap the query → return "No — explicitly stated."

μ=0: pure regex + dict. No LLM, no statistics.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Negation markers — the linguistic surface forms of "not".
# Order matters: longer phrases first ("no longer" before "no").
# ---------------------------------------------------------------------------
NEGATION_MARKERS: List[str] = [
    # Verb-attached negation
    "don't", "doesn't", "didn't", "isn't", "wasn't", "aren't",
    "weren't", "won't", "wouldn't", "can't", "couldn't", "shouldn't",
    "shant", "shan't", "mustn't", "hasn't", "haven't", "hadn't",
    # Multi-word
    "do not", "does not", "did not", "is not", "was not", "are not",
    "were not", "will not", "would not", "cannot", "could not",
    "should not", "must not", "has not", "have not", "had not",
    # Adverbial
    "no longer", "not any more", "not anymore", "no more",
    "never", "nowhere", "neither", "nor",
    # Cessation
    "stopped", "quit", "gave up", "no longer", "ceased",
    "discontinued", "abandoned",
    # Single-word
    "not", "no",
]

# Build a master regex of all markers, longest-first, case-insensitive,
# word-bounded.
_MARKERS_SORTED = sorted(NEGATION_MARKERS, key=len, reverse=True)
_MARKERS_RE = re.compile(
    rf"\b(?:{'|'.join(re.escape(m) for m in _MARKERS_SORTED)})\b",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Sentence-level negation detection
# ---------------------------------------------------------------------------

# Use the existing tokenizer.sentence splitter for consistency.
def _split_sentences(text: str) -> List[Tuple[int, int, str]]:
    """Return [(start, end, sentence)] spans. Delegates to tokenizer."""
    from cortexm.text.tokenizer import sentences
    return sentences(text)


def detect_negation(text: str) -> List[Dict]:
    """Detect negated sentences in text.

    Returns a list of dicts, each with:
      - ``sentence``: the negated sentence
      - ``marker``: the negation marker matched (e.g. "don't")
      - ``marker_span``: (start, end) of the marker within the sentence
      - ``sentence_span``: (start, end) of the sentence within the text
      - ``implied_subject``: heuristic guess of what's being negated
        (the first noun-phrase after the marker; may be empty)
    """
    out: List[Dict] = []
    if not text:
        return out
    for s_start, s_end, sentence in _split_sentences(text):
        m = _MARKERS_RE.search(sentence)
        if not m:
            continue
        # Heuristic implied subject: the words between the sentence
        # start and the negation marker. For "I don't eat meat",
        # the implied subject is "I" and the negated verb is "eat".
        # For "I no longer work at Google", implied subject is "I"
        # and negated verb is "work at Google".
        prefix = sentence[:m.start()].strip()
        implied_subject = prefix.split()[-1] if prefix.split() else ""
        out.append({
            "sentence": sentence,
            "marker": m.group(0),
            "marker_span": (s_start + m.start(), s_start + m.end()),
            "sentence_span": (s_start, s_end),
            "implied_subject": implied_subject,
        })
    return out


def extract_with_negation(text: str) -> Dict:
    """Split text into non-negated and negated sentences.

    Returns:
        {
            "positive_text": str,    # concatenation of non-negated sentences
            "negations": [...],      # output of detect_negation()
        }

    The structured extractor should run on ``positive_text`` only.
    The negations should be written to a separate
    ``negation_records`` table (see store.py's schema).
    """
    negations = detect_negation(text)
    if not negations:
        return {"positive_text": text, "negations": []}
    # Sort negations by start so we can splice out the spans
    spans_to_remove = sorted(
        (n["sentence_span"] for n in negations), key=lambda s: s[0])
    # Build the positive text by skipping the negated sentence spans
    out: List[str] = []
    last_end = 0
    for s, e in spans_to_remove:
        out.append(text[last_end:s])
        last_end = e
    out.append(text[last_end:])
    positive = " ".join(out)
    positive = re.sub(r"\s{2,}", " ", positive).strip()
    return {"positive_text": positive, "negations": negations}


def is_negation_overlap(query: str, negation_record: Dict) -> bool:
    """Heuristic: does the query overlap the negation record?

    Used by the reader to decide whether a negation record contradicts
    the query. Returns True if ANY content word in the query appears in
    the negation record's sentence.

    Conservative: better to return False (and let the reader fall
    through to positive facts) than to return True and suppress a
    legitimate positive answer.
    """
    from cortexm.text.tokenizer import content_words
    q_words = set(content_words(query))
    if not q_words:
        return False
    sent_words = set(content_words(negation_record.get("sentence", "")))
    overlap = q_words & sent_words
    # Require at least 2 content-word overlaps (one is too noisy)
    return len(overlap) >= 2


# ---------------------------------------------------------------------------
# SQL schema for the negation_records table. The store applies this
# via executescript() on init (idempotent).
# ---------------------------------------------------------------------------
NEGATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS negation_records (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  agent_id TEXT,
  session_id TEXT,
  sentence TEXT NOT NULL,
  marker TEXT NOT NULL,
  implied_subject TEXT,
  source_tx_id TEXT,
  source_hash TEXT,
  created_at TEXT NOT NULL,
  is_active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_neg_user ON negation_records(user_id);
CREATE INDEX IF NOT EXISTS idx_neg_subject ON negation_records(implied_subject);
CREATE INDEX IF NOT EXISTS idx_neg_active ON negation_records(is_active);
"""

# Schema helpers — for the VerbatimPlugin or MemoryWriter to call
# when they get a SQLite connection. The negation_records table lives
# in the SAME .db file as facts + verbatim_chunks.
