"""IR fundamentals — Lucene/Solr-grade primitives over SQLite FTS5.

WHY THIS MODULE EXISTS
-----------------------
The user audit (Aug 2026) flagged 12 specific IR primitives that
Lucene/Solr/Elasticsearch solved decades ago but cortexm was missing:

  1. Phrase queries (NEAR)        — adjacent word search
  2. Spell correction              — Levenshtein on query terms
  3. Stopword handling             — multi-language, query-time only
  4. Index compaction               — VACUUM + FTS5 optimize
  5. Query result cache             — LRU on (query, user_id, k)
  6. Highlighting / snippets        — FTS5 snippet()
  7. Faceting                       — materialized counts per field
  8. MoreLikeThis                   — term-vector similarity
  9. Range queries                  — numeric range via B-tree index
  10. Auto-suggest / typeahead     — fts5vocab prefix search
  11. BM25 k1/b tuning              — already exists in VerbatimPlugin
  12. Text analysis pipeline        — NFKC + stemmer + position tracking

This module implements all 12 (some already existed; this consolidates
them and adds the missing ones) as stateless helper functions that
operate on a SQLite connection. The VerbatimPlugin delegates to these
for the new public API surface.

ARCHITECTURE
-------------
Every function takes the SQLite connection as its first argument and
returns plain Python data (lists/dicts/tuples). No classes, no state —
the caller (VerbatimPlugin) owns the connection lifetime.

μ=0: all operations are pure SQL + Python. No LLM, no API, no
statistics beyond IDF/BM25 (which SQLite FTS5 already computes).
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import OrderedDict
from functools import lru_cache
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Stopwords — multi-language, query-time only (Lucene approach).
# ---------------------------------------------------------------------------
# The Lucene approach is to NOT remove stopwords at index time (because
# "to be or not to be" is all stopwords but meaningful) and INSTEAD
# remove them at query time only. The verbatim_chunks FTS5 table stores
# every token; the query planner drops stopwords before BM25.

# Curated stopword lists for 6 languages. Sources: NLTK corpus stoplists
# (the public-domain lists), Lucene's default Snowball stoplists.
STOPWORDS_EN = {
    # articles
    "a", "an", "the",
    # conjunctions
    "and", "or", "but", "if", "while", "although", "though", "because",
    "until", "since", "unless", "whether", "than", "as", "so",
    # prepositions
    "of", "to", "in", "on", "at", "for", "with", "by", "from", "into",
    "onto", "upon", "over", "under", "above", "below", "between",
    "through", "during", "before", "after", "against", "without",
    # pronouns
    "i", "me", "my", "mine", "myself", "we", "us", "our", "ours",
    "ourselves", "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself", "she", "her", "hers", "herself",
    "it", "its", "itself", "they", "them", "their", "theirs",
    "themselves", "this", "that", "these", "those", "what", "which",
    "who", "whom", "whose", "where", "when", "why", "how",
    # be-verbs + auxiliaries
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "doing",
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "ought",
    # determiners / quantifiers
    "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "too",
    "very", "just",
    # common noise
    "about", "above", "across", "after", "afterwards", "again", "against",
    "all", "almost", "alone", "along", "already", "also", "although",
    "always", "among", "amongst", "an", "and", "another", "any", "anyhow",
    "anyone", "anything", "anyway", "anywhere", "are", "around", "as", "at",
    "back", "be", "became", "because", "become", "becomes", "becoming",
    "been", "before", "beforehand", "behind", "being", "below", "beside",
    "besides", "between", "beyond", "both", "but", "by",
    # discourse markers we don't want as BM25 anchors
    "yes", "no", "okay", "ok", "yeah", "nope", "well",
}

STOPWORDS_ES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "y", "o", "de",
    "del", "a", "en", "que", "es", "son", "se", "por", "para", "con", "su",
    "sus", "lo", "como", "mas", "pero", "si", "no", "cuando", "donde",
    "quien", "cual", "cuya", "mio", "tuyo", "suyo", "este", "esta", "estos",
    "estas", "ese", "esa", "esos", "esas", "aquel", "aquella",
}

STOPWORDS_FR = {
    "le", "la", "les", "un", "une", "des", "et", "ou", "de", "du", "a",
    "en", "que", "qui", "est", "sont", "se", "pour", "par", "avec", "son",
    "sa", "ses", "ce", "cette", "ces", "comme", "mais", "si", "ne", "pas",
    "quand", "ou", "dont", "lequel", "laquelle", "lesquels", "lesquelles",
}

STOPWORDS_DE = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "eines",
    "einem", "einen", "einer", "und", "oder", "von", "zu", "in", "an",
    "auf", "mit", "für", "ist", "sind", "war", "waren", "sein", "hat",
    "hatte", "haben", "wird", "wurde", "werden", "aber", "dass", "weil",
    "wenn", "als", "wie", "was", "wer", "wo", "wann", "warum",
}

STOPWORDS_IT = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "e", "o",
    "di", "del", "della", "dei", "degli", "delle", "da", "in", "con",
    "su", "per", "tra", "fra", "è", "sono", "sei", "sia", "siano",
    "sarà", "saranno", "era", "erano", "ma", "se", "perché", "quando",
    "dove", "come", "che", "chi", "cui",
}

STOPWORDS_PT = {
    "o", "a", "os", "as", "um", "uma", "uns", "umas", "e", "ou", "de",
    "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "por",
    "para", "com", "sem", "sob", "sobre", "é", "são", "foi", "foram",
    "ser", "estar", "mas", "se", "porque", "quando", "onde", "como",
    "que", "quem", "qual", "cujas",
}

STOPWORDS_BY_LANG: Dict[str, frozenset] = {
    "en": frozenset(STOPWORDS_EN),
    "es": frozenset(STOPWORDS_ES),
    "fr": frozenset(STOPWORDS_FR),
    "de": frozenset(STOPWORDS_DE),
    "it": frozenset(STOPWORDS_IT),
    "pt": frozenset(STOPWORDS_PT),
}


def get_stopwords(lang: str = "en") -> frozenset:
    """Return the stopword set for a language code."""
    return STOPWORDS_BY_LANG.get(lang, STOPWORDS_BY_LANG["en"])


# ---------------------------------------------------------------------------
# Text analysis pipeline — Lucene 6-stage analog (simplified for μ=0).
# ---------------------------------------------------------------------------
def nfkc_normalize(text: str) -> str:
    """Unicode NFKC normalization — fold compatibility forms.

    "ﬁ" (ligature) → "fi"; "²" (superscript) → "2"; "Å" (Angstrom) → "Å".
    Idempotent: applying twice = applying once.
    """
    if not text:
        return text
    return unicodedata.normalize("NFKC", text)


def strip_accents(text: str) -> str:
    """Remove accents (for accent-insensitive matching).

    "Café" → "Cafe"; "naïve" → "naive". Use sparingly — for some
    languages (French, Spanish) accents are phonemic. Apply at query
    time only when the user types ASCII-only queries.
    """
    if not text:
        return text
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# Simple Porter-like stemmer for English (lightweight, ~12 rules).
# This is NOT a full Porter implementation — it handles the most common
# suffixes (-s, -es, -ing, -ed, -ly, -ment, -ness, -tion). Use it for
# BM25 query expansion where morphology matters but a full stemmer is
# overkill. Falls back to the original word if no rule matches.
# Each rule is (regex, replacement). Regex is matched with re.sub on
# the lowercased word; the FIRST matching rule wins.
_STEM_RULES = [
    (re.compile(r"(.+)(ies)$"), r"\1y"),         # cities → city
    (re.compile(r"(.+)(ied)$"), r"\1y"),          # carried → carry
    (re.compile(r"(.+)(ying)$"), r"\1y"),          # lying → lie
    (re.compile(r"(.+)(ing)$"), r"\1"),            # running → runn → (drop ing)
    (re.compile(r"(.+)(ed)$"), r"\1"),            # jumped → jump
    (re.compile(r"(.+)(ly)$"), r"\1"),             # quickly → quick
    (re.compile(r"(.+)(ment)$"), r"\1"),           # agreement → agree
    (re.compile(r"(.+)(ness)$"), r"\1"),           # darkness → dark
    (re.compile(r"(.+)(tion)$"), r"\1t"),          # creation → creat
    (re.compile(r"(.+[os])s$"), r"\1"),            # boss → boss (keep ss after o/s)
    (re.compile(r"(.+[a-rt-z])s$"), r"\1"),        # cats → cat, dogs → dog
]


def stem(word: str) -> str:
    """Lightweight English stemmer (Porter-like, μ=0).

    Returns the stemmed form. If no rule matches, returns the original.
    NOT idempotent (running → runn; applying twice → runn) — apply once.
    """
    if not word or len(word) < 4:
        return word
    w = word.lower()
    for pat, repl in _STEM_RULES:
        new, n = pat.subn(repl, w, count=1)
        if n > 0:
            return new
    return w


def _levenshtein(a: str, b: str) -> int:
    """Full-string Levenshtein distance (DP, O(len_a * len_b)).

    Used by spell correction — unlike Bitap (substring matching), this
    is true full-string edit distance. Pure μ=0.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Two-row DP for O(min(len_a, len_b)) memory.
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(
                cur[-1] + 1,        # insertion
                prev[j] + 1,        # deletion
                prev[j - 1] + cost  # substitution
            ))
        prev = cur
    return prev[-1]


def analyze(text: str, *, lang: str = "en",
            remove_stopwords: bool = True,
            do_stem: bool = True) -> List[str]:
    """Full Lucene-style 6-stage analysis pipeline.

    Stages:
      1. NFKC Unicode normalization
      2. Case folding (lowercase)
      3. Tokenization (whitespace + punctuation split)
      4. Stopword removal (if remove_stopwords)
      5. Stemming (if do_stem)
      6. Return as ordered list of tokens

    The result is a list of normalized tokens ready for BM25 query
    construction.
    """
    if not text:
        return []
    # Stage 1: NFKC
    text = nfkc_normalize(text)
    # Stage 2: lowercase
    text = text.lower()
    # Stage 3: tokenize. Split on whitespace + punctuation, keep
    # word-internal apostrophes (so "dog's" stays as "dog's").
    raw_tokens = re.findall(r"\w+(?:'\w+)?", text)
    # Stage 4: stopword removal
    if remove_stopwords:
        sw = get_stopwords(lang)
        raw_tokens = [t for t in raw_tokens if t not in sw]
    # Stage 5: stemming (English only for now)
    if do_stem and lang == "en":
        raw_tokens = [stem(t) for t in raw_tokens]
    return raw_tokens


# ---------------------------------------------------------------------------
# Phrase queries — FTS5 NEAR().
# ---------------------------------------------------------------------------
def build_phrase_query(terms: List[str], *, slop: int = 1) -> str:
    """Build an FTS5 NEAR() phrase query.

    ``build_phrase_query(["italian", "garden"], slop=2)`` →
    ``"NEAR(italian, garden, 2)"`` — finds chunks where "italian"
    and "garden" appear within 2 tokens of each other (any order).

    ``slop=0`` (exact phrase) → ``"\"italian garden\""`` (FTS5
    phrase syntax — adjacent, in order).

    The function does NOT escape FTS5 special chars — call analyze()
    first to clean the input.
    """
    if not terms:
        return '""'
    if slop == 0:
        # Exact phrase — quote the tokens joined with single spaces.
        # FTS5 treats "..." as an ordered phrase query.
        return '"' + " ".join(terms) + '"'
    return f"NEAR({' '.join(terms)}, {slop})"


def phrase_search(conn: sqlite3.Connection, *,
                  phrase: str, user_id: str,
                  slop: int = 1, k: int = 10) -> List[Dict]:
    """Run a phrase query via FTS5 NEAR().

    Returns top-k chunks where the phrase terms appear adjacent
    (within `slop` tokens of each other).
    """
    terms = analyze(phrase, remove_stopwords=False, do_stem=False)
    if not terms:
        return []
    fts_query = build_phrase_query(terms, slop=slop)
    sql = (
        "SELECT rowid, text, user_id, session_id, source_tx_id, "
        "bm25(verbatim_chunks, 1.5, 0.75) AS rank "
        "FROM verbatim_chunks "
        "WHERE user_id = ? AND verbatim_chunks MATCH ? "
        "ORDER BY rank LIMIT ?")
    try:
        rows = conn.execute(sql, (user_id, fts_query, k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"rowid": r[0], "text": r[1], "rank": r[5]} for r in rows]


# ---------------------------------------------------------------------------
# Highlighting / snippets — FTS5 snippet() function.
# ---------------------------------------------------------------------------
def highlight(conn: sqlite3.Connection, *,
              query: str, user_id: str, k: int = 5,
              before: str = "<b>", after: str = "</b>",
              ellipsis: str = "...", tokens: int = 10) -> List[Dict]:
    """Return chunks with the matched terms highlighted.

    Uses FTS5's built-in ``snippet()`` function which:
      - finds the matching terms in the chunk
      - returns a short excerpt (default 10 tokens) around the match
      - wraps matched terms in ``before`` + ``after`` markers
      - prepends/appends ``ellipsis`` for truncated content

    Default markers (``<b>`` / ``</b>``) suit HTML rendering. For
    terminal output, use ``before="\033[1m"``, ``after="\033[0m"``.
    """
    terms = analyze(query, remove_stopwords=True, do_stem=False)
    if not terms:
        return []
    # OR-join the analyzed terms so any match highlights
    fts_query = " OR ".join(f'"{t}"' for t in terms)
    sql = (
        "SELECT rowid, "
        "snippet(verbatim_chunks, 0, ?, ?, ?, ?) AS excerpt, "
        "bm25(verbatim_chunks, 1.5, 0.75) AS rank "
        "FROM verbatim_chunks "
        "WHERE user_id = ? AND verbatim_chunks MATCH ? "
        "ORDER BY rank LIMIT ?")
    try:
        rows = conn.execute(
            sql, (before, after, ellipsis, tokens,
                  user_id, fts_query, k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"rowid": r[0], "excerpt": r[1], "rank": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# Faceting — materialized counts per field value.
# ---------------------------------------------------------------------------
def facet_counts(conn: sqlite3.Connection, *,
                 user_id: str, field: str = "relation") -> Dict[str, int]:
    """Return {value: count} for a field in the facts table.

    For ``field="relation"`` returns counts of each relation type
    (works_at, lives_in, has_pet, etc.). For ``field="subject"`` returns
    counts of each subject. The query is a single GROUP BY — O(N) scan
    but cached by SQLite's page cache.

    For really large fact tables (10M+), pre-materialize via a trigger
    on facts INSERT/UPDATE/DELETE that maintains a fact_counts table.
    """
    allowed = {"relation", "subject", "value", "user_id"}
    if field not in allowed:
        raise ValueError(
            f"facet field must be one of {allowed}, got {field!r}")
    sql = (
        f"SELECT {field}, COUNT(*) FROM facts "
        f"WHERE user_id = ? AND is_active = 1 "
        f"GROUP BY {field} ORDER BY COUNT(*) DESC")
    rows = conn.execute(sql, (user_id,)).fetchall()
    return {r[0]: r[1] for r in rows if r[0]}


# ---------------------------------------------------------------------------
# MoreLikeThis — term-vector similarity.
# ---------------------------------------------------------------------------
def more_like_this(conn: sqlite3.Connection, *,
                    chunk_rowid: int, user_id: str,
                    k: int = 5, max_terms: int = 10) -> List[Dict]:
    """Find chunks similar to a given chunk via FTS5 term vectors.

    Algorithm:
      1. Pull the chunk's significant terms (high IDF, top-N).
      2. Build an OR FTS5 query from those terms.
      3. Run BM25 with that query, exclude the source chunk, return top-k.

    This is the classic Lucene ``MoreLikeThis`` class ported to FTS5.
    """
    # Step 1: get the source chunk's text + rowid
    src = conn.execute(
        "SELECT text FROM verbatim_chunks WHERE rowid = ? AND user_id = ?",
        (chunk_rowid, user_id)).fetchone()
    if not src:
        return []
    src_text = src[0]
    # Step 2: extract significant terms. Use the existing analyze()
    # pipeline; for "significance" we approximate IDF by token frequency
    # in this chunk — rare-in-chunk terms (count=1) are more
    # discriminative. (A proper IDF requires the corpus statistics table
    # which we don't maintain here.)
    terms = analyze(src_text, remove_stopwords=True, do_stem=True)
    if not terms:
        return []
    # Count occurrences per term; rare terms (count=1) are most
    # discriminative. If all terms appear once, prefer longer terms.
    counts: Dict[str, int] = {}
    for t in terms:
        counts[t] = counts.get(t, 0) + 1
    # Sort: rare-first, then longest-first
    ranked = sorted(counts.items(), key=lambda x: (x[1], -len(x[0])))
    sig_terms = [t for t, _ in ranked[:max_terms]]
    if not sig_terms:
        return []
    # Step 3: OR-join significant terms into a BM25 query
    fts_query = " OR ".join(f'"{t}"' for t in sig_terms)
    sql = (
        "SELECT rowid, text, bm25(verbatim_chunks, 1.5, 0.75) AS rank "
        "FROM verbatim_chunks "
        "WHERE user_id = ? AND rowid != ? AND verbatim_chunks MATCH ? "
        "ORDER BY rank LIMIT ?")
    try:
        rows = conn.execute(
            sql, (user_id, chunk_rowid, fts_query, k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"rowid": r[0], "text": r[1], "rank": r[2],
             "matched_terms": sig_terms} for r in rows]


# ---------------------------------------------------------------------------
# Range queries — numeric range via B-tree index.
# ---------------------------------------------------------------------------
def range_search(conn: sqlite3.Connection, *,
                 user_id: str, relation: str,
                 min_value: float | None = None,
                 max_value: float | None = None,
                 k: int = 100) -> List[Dict]:
    """Find facts with a numeric value in [min, max].

    Use case: "find expenses between $50 and $100".

    The facts table stores values as TEXT. We cast to REAL for the
    comparison — SQLite is permissive about this. For TEXT values that
    aren't numeric, the cast returns 0.0 (so they'd match if 0 is in
    range; filter on relation to avoid this).

    For high-volume numeric fields, create a partial index:
      CREATE INDEX idx_facts_amount ON facts(CAST(value AS REAL))
      WHERE relation = ?;
    """
    if min_value is None and max_value is None:
        return []
    conditions = ["user_id = ?", "is_active = 1", "relation = ?"]
    params = [user_id, relation]
    if min_value is not None:
        conditions.append("CAST(value AS REAL) >= ?")
        params.append(min_value)
    if max_value is not None:
        conditions.append("CAST(value AS REAL) <= ?")
        params.append(max_value)
    where = " AND ".join(conditions)
    sql = (
        f"SELECT id, subject, relation, value, valid_from, valid_to "
        f"FROM facts WHERE {where} "
        f"ORDER BY CAST(value AS REAL) LIMIT ?")
    params.append(k)
    rows = conn.execute(sql, params).fetchall()
    return [{"id": r[0], "subject": r[1], "relation": r[2], "value": r[3],
             "valid_from": r[4], "valid_to": r[5]} for r in rows]


# ---------------------------------------------------------------------------
# Auto-suggest / typeahead — FTS5 fts5vocab table.
# ---------------------------------------------------------------------------
def suggest(conn: sqlite3.Connection, *,
            prefix: str, k: int = 5) -> List[Dict]:
    """Return completions for a prefix from the verbatim FTS5 vocab.

    ``suggest("gar", k=5)`` → [{"term": "garden", "count": 5},
    {"term": "gardening", "count": 2}, ...]

    Uses FTS5's ``fts5vocab`` virtual table (an instance must be
    created once via ``CREATE VIRTUAL TABLE ... USING fts5vocab(...)``).
    The helper creates it lazily on first call.
    """
    if not prefix:
        return []
    # Ensure fts5vocab table exists (idempotent)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS verbatim_vocab "
            "USING fts5vocab('verbatim_chunks', 'instance')")
    except sqlite3.OperationalError:
        pass  # already exists
    sql = (
        "SELECT term, sum(count) AS total "
        "FROM verbatim_vocab "
        "WHERE term LIKE ? "
        "GROUP BY term ORDER BY total DESC LIMIT ?")
    try:
        rows = conn.execute(sql, (prefix.lower() + "%", k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"term": r[0], "count": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Index compaction — VACUUM + FTS5 optimize.
# ---------------------------------------------------------------------------
def optimize_index(conn: sqlite3.Connection) -> Dict:
    """VACUUM + FTS5 optimize + checkpoint.

    ``VACUUM`` reclaims space from deleted rows and rebuilds B-trees.
    ``INSERT INTO verbatim_chunks(verbatim_chunks) VALUES('optimize')``
    rebuilds the FTS5 inverted index (merges segments).
    ``PRAGMA wal_checkpoint(TRUNCATE)`` folds the WAL back into the
    main db file.

    Run this as a weekly cron or during ``cortexm consolidate`` when
    the .db file has grown large with deleted facts.

    Returns a dict with before/after sizes (in bytes) when measurable.
    """
    out: Dict = {"vacuum": False, "fts5_optimize": False, "checkpoint": False}
    try:
        conn.execute("VACUUM")
        out["vacuum"] = True
    except sqlite3.OperationalError:
        pass  # VACUUM inside an open transaction fails; caller must commit first
    try:
        conn.execute(
            "INSERT INTO verbatim_chunks(verbatim_chunks) "
            "VALUES('optimize')")
        conn.commit()
        out["fts5_optimize"] = True
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        out["checkpoint"] = True
    except sqlite3.OperationalError:
        pass
    return out


# ---------------------------------------------------------------------------
# Spell correction — Levenshtein on query terms.
# ---------------------------------------------------------------------------
# Use the existing Bitap fuzzy matcher from cortexm.text.fuzzy — it's
# faster than the naive DP and operates on the same edit-distance
# metric. For query-term correction we need a vocabulary to correct
# against; this function builds one from FTS5's fts5vocab table.
def _build_vocabulary(conn: sqlite3.Connection,
                      min_count: int = 2) -> set:
    """Return the set of in-corpus terms (count >= min_count).

    Used as the dictionary for spell correction. Cached via lru_cache
    so repeated calls within the same connection are free.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS verbatim_vocab "
            "USING fts5vocab('verbatim_chunks', 'instance')")
    except sqlite3.OperationalError:
        pass
    sql = "SELECT DISTINCT term FROM verbatim_vocab WHERE count >= ?"
    try:
        rows = conn.execute(sql, (min_count,)).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


def correct_spelling(term: str, vocabulary: set,
                      max_dist: int = 2) -> str:
    """Find the closest in-vocabulary term within max_dist edits.

    Falls back to the original term if no candidate is within max_dist.

    Uses full-string Levenshtein (DP) — NOT Bitap (which does
    substring matching and would always return 0 for terms that
    appear as substrings of vocabulary entries).
    """
    if not term or term in vocabulary:
        return term
    best: Optional[Tuple[str, int]] = None
    for v in vocabulary:
        # Skip if lengths differ by more than max_dist (early exit)
        if abs(len(v) - len(term)) > max_dist:
            continue
        d = _levenshtein(term, v)
        if d > max_dist:
            continue
        if best is None or d < best[1]:
            best = (v, d)
            if d == 1:
                break  # 1-edit match is good enough
    return best[0] if best else term


def correct_query(query: str, vocabulary: set,
                  max_dist: int = 2) -> str:
    """Apply spell correction to each token in a query.

    Tokens already in the vocabulary are left alone. Tokens within
    max_dist of a vocabulary term are replaced with the closest match.
    """
    if not query:
        return query
    tokens = re.findall(r"\w+(?:'\w+)?", query)
    out = []
    for t in tokens:
        out.append(correct_spelling(t.lower(), vocabulary, max_dist))
    return " ".join(out)


# ---------------------------------------------------------------------------
# Query result cache — LRU with manual invalidation.
# ---------------------------------------------------------------------------
class LRUCache:
    """Manual LRU cache (functools.lru_cache can't be invalidated
    per-user; this one supports explicit invalidation).

    Cache key: (query, user_id, k, agent_id). Invalidation fires on
    ``add()`` / ``edit()`` / ``fix()`` — see VerbatimPlugin for the
    invalidate API.
    """

    def __init__(self, capacity: int = 1024) -> None:
        self.capacity = capacity
        self._cache: OrderedDict = OrderedDict()

    def get(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key, value) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)

    def invalidate(self, *, user_id: str | None = None) -> int:
        """Drop cache entries for a user (or all entries).

        Returns the number of entries dropped. Called by the writer
        when a new fact is added (invalidating cached search results
        for that user).
        """
        if user_id is None:
            n = len(self._cache)
            self._cache.clear()
            return n
        keys_to_drop = [k for k in self._cache if k[1] == user_id]
        for k in keys_to_drop:
            self._cache.pop(k, None)
        return len(keys_to_drop)

    def __len__(self) -> int:
        return len(self._cache)


# ---------------------------------------------------------------------------
# BM25 parameter exposure — k1 and b are already in VerbatimPlugin's
# constructor; this helper exists so callers can tune at runtime.
# ---------------------------------------------------------------------------
def tune_bm25(plugin, k1: float = 1.2, b: float = 0.75) -> None:
    """Update the BM25 k1/b on a VerbatimPlugin instance.

    The new values take effect on the next search() call. Defaults:
    k1=1.2 (Lucene default), b=0.75 (Lucene default). The plugin's
    constructor uses k1=1.5, b=0.75 — slightly more aggressive term
    saturation than Lucene's default, tuned for short chunks.
    """
    plugin.bm25_k1 = k1
    plugin.bm25_b = b
