"""Deterministic tokenizer & sentence segmentation (μ=0 building block)."""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'’-]*|\d+(?:\.\d+)?")
ABBREV = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
          "e.g", "i.e", "fig", "inc", "ltd", "co", "u.s", "u.k"}

_SENT_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n{2,}")
_SENT_GUARD = re.compile(r"^(?:[A-Z0-9\"'(]|…)")


def words(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in WORD_RE.findall(text)]


def sentences(text: str) -> list[tuple[int, int, str]]:
    """Sentence segmentation with (start, end, sentence) spans."""
    out: list[tuple[int, int, str]] = []
    if not text or not text.strip():
        return out
    pos = 0
    for raw in _SENT_SPLIT.split(text):
        s = raw.strip()
        if not s:
            continue
        start = text.find(s[:24], pos)
        if start < 0:
            start = pos
        end = start + len(s)
        out.append((start, end, s))
        pos = end
    # merge fragments ending in abbreviations ("I met Dr. Chen.")
    merged: list[tuple[int, int, str]] = []
    for item in out:
        if merged:
            ps, pe, ptext = merged[-1]
            tail = re.sub(r"[^\w.]", "", ptext.split()[-1] if ptext.split() else "")
            if tail.rstrip(".").lower() in ABBREV:
                nxt = text[pe:item[1]]
                merged[-1] = (ps, item[1], text[ps:item[1]])
                continue
        merged.append(item)
    return merged


def strip_punct(s: str) -> str:
    return re.sub(r"[^\w\s'-]", " ", s).strip()


STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "is", "are", "was", "were", "be", "been", "am", "do",
    "does", "did", "have", "has", "had", "i", "you", "he", "she", "it", "we",
    "they", "me", "my", "your", "his", "her", "its", "our", "their", "this",
    "that", "these", "those", "as", "so", "than", "then", "there", "here",
    "what", "which", "who", "whom", "when", "where", "why", "how", "will",
    "would", "can", "could", "should", "shall", "may", "might", "just",
    "about", "from", "by", "up", "out", "not", "no", "yes", "oh", "well",
}

# Fallback: merge with sklearn's stopwords if available (larger, domain-aware set)
try:
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
    STOPWORDS = STOPWORDS | set(ENGLISH_STOP_WORDS)
except ImportError:
    pass


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS and len(w) > 1]


def cap_sequences(text: str) -> list[str]:
    """Capitalized multi-word sequences — poor-man's NER (μ=0)."""
    seqs = re.findall(
        r"\b([A-Z][a-zA-Z'&-]*(?:[ ](?:of|the|and|de|van|for)[ ])?[ ]*[A-Z][a-zA-Z'&-]*)+\b",
        text)
    out = []
    for s in seqs:
        s = " ".join(s.split())
        if len(s) > 2 and not s.islower() and "." not in s:
            out.append(s)
    return out
