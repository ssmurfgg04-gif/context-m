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

# Vendored copy of sklearn's ENGLISH_STOP_WORDS (318 words, stable across
# sklearn versions). Imported lazily before via try/except, but that pulled
# all of sklearn+scipy (~8-10 s on Windows) into every `cortexm serve`
# startup — blowing past opencode's 10 s MCP timeout. A static literal costs
# ~0 ms and is *more* deterministic (no cross-version drift). Source:
# sklearn.feature_extraction.text.ENGLISH_STOP_WORDS (verified 1.9.0).
_SKLEARN_STOPWORDS = frozenset({
    "a", "about", "above", "across", "after", "afterwards", "again",
    "against", "all", "almost", "alone", "along", "already", "also",
    "although", "always", "am", "among", "amongst", "amoungst", "amount",
    "an", "and", "another", "any", "anyhow", "anyone", "anything",
    "anyway", "anywhere", "are", "around", "as", "at", "back", "be",
    "became", "because", "become", "becomes", "becoming", "been",
    "before", "beforehand", "behind", "being", "below", "beside",
    "besides", "between", "beyond", "bill", "both", "bottom", "but",
    "by", "call", "can", "cannot", "cant", "co", "con", "could",
    "couldnt", "cry", "de", "describe", "detail", "do", "done", "down",
    "due", "during", "each", "eg", "eight", "either", "eleven", "else",
    "elsewhere", "empty", "enough", "etc", "even", "ever", "every",
    "everyone", "everything", "everywhere", "except", "few", "fifteen",
    "fifty", "fill", "find", "fire", "first", "five", "for", "former",
    "formerly", "forty", "found", "four", "from", "front", "full",
    "further", "get", "give", "go", "had", "has", "hasnt", "have", "he",
    "hence", "her", "here", "hereafter", "hereby", "herein", "hereupon",
    "hers", "herself", "him", "himself", "his", "how", "however",
    "hundred", "i", "ie", "if", "in", "inc", "indeed", "interest",
    "into", "is", "it", "its", "itself", "keep", "last", "latter",
    "latterly", "least", "less", "ltd", "made", "many", "may", "me",
    "meanwhile", "might", "mill", "mine", "more", "moreover", "most",
    "mostly", "move", "much", "must", "my", "myself", "name", "namely",
    "neither", "never", "nevertheless", "next", "nine", "no", "nobody",
    "none", "noone", "nor", "not", "nothing", "now", "nowhere", "of",
    "off", "often", "on", "once", "one", "only", "onto", "or", "other",
    "others", "otherwise", "our", "ours", "ourselves", "out", "over",
    "own", "part", "per", "perhaps", "please", "put", "rather", "re",
    "same", "see", "seem", "seemed", "seeming", "seems", "serious",
    "several", "she", "should", "show", "side", "since", "sincere",
    "six", "sixty", "so", "some", "somehow", "someone", "something",
    "sometime", "sometimes", "somewhere", "still", "such", "system",
    "take", "ten", "than", "that", "the", "their", "them", "themselves",
    "then", "thence", "there", "thereafter", "thereby", "therefore",
    "therein", "thereupon", "these", "they", "thick", "thin", "third",
    "this", "those", "though", "three", "through", "throughout",
    "thru", "thus", "to", "together", "too", "top", "toward",
    "towards", "twelve", "twenty", "two", "un", "under", "until", "up",
    "upon", "us", "very", "via", "was", "we", "well", "were", "what",
    "whatever", "when", "whence", "whenever", "where", "whereafter",
    "whereas", "whereby", "wherein", "whereupon", "wherever", "whether",
    "which", "while", "whither", "who", "whoever", "whole", "whom",
    "whose", "why", "will", "with", "within", "without", "would",
    "yet", "you", "your", "yours", "yourself", "yourselves",
})
STOPWORDS = STOPWORDS | _SKLEARN_STOPWORDS


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
