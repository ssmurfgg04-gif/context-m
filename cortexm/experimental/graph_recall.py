"""Entity→fact adjacency index with bounded graph walks (μ=0).

Borrowed from RuVector's graph-memory reconstruction ("follow
Cue → Tag → Content instead of one embedding search"), stripped to a
deterministic core:

  * an inverted index maps entity tokens (from fact subjects and
    values) to the fact ids they participate in;
  * a query's tokens that HIT the index anchor a bounded BFS walk
    (default 2 hops) that pulls facts CONNECTED to the query's
    entities even when lexical/VSA similarity to the fact's triple is
    low — the multi-session failure mode where a similar-looking fact
    from the WRONG session wins on embedding similarity alone;
  * facts reached through MULTIPLE distinct query tokens score higher
    (cross-confirmation), and each hop decays the contribution.

Why this is Pareto for cortexm: the retrieval misses it targets do
not need semantic similarity at all — they need the join structure
the extractor already produced. The index is one pass over the facts
table; the walk is set intersection. No LLM, no learned weights,
byte-deterministic output.
"""
from __future__ import annotations

import threading
from typing import Any

# Minimal stopword set — kept local so the module has zero deps on
# the bench/judge layer (which lives under scripts/).
_STOPWORDS = frozenset("""
a an and are as at be been by did do does for from had has have how i
in is it its me my not of on or our so that the their them they this
to was we were what when where which who why will with you your
just got get very really much many name named called
""".split())

_MIN_TOKEN_LEN = 3


def _entity_tokens(subject: str, value: str) -> list[str]:
    """Deterministic entity tokens for one fact triple.

    Subject: the raw subject plus its ``user:``-stripped form.
    Value: whitespace/punctuation-split tokens with stopwords and
    short tokens dropped (so ``"dog named Charlie"`` yields
    ``dog, charlie``).
    """
    toks: list[str] = []
    subj = (subject or "").strip()
    if subj:
        toks.append(subj.lower())
        if ":" in subj:
            toks.append(subj.split(":", 1)[1].lower())
    val = (value or "").strip()
    if val:
        for piece in val.replace(",", " ").replace(".", " ").split():
            piece = piece.strip("'\"!?;:()[]").lower()
            if (len(piece) >= _MIN_TOKEN_LEN
                    and piece not in _STOPWORDS
                    and piece.isalnum()):
                toks.append(piece)
    return toks


class EntityGraphIndex:
    """Inverted entity-token → fact-id adjacency index.

    Built lazily from the store's ACTIVE, non-quarantined facts for a
    user; cached and rebuilt automatically when the underlying active
    fact count changes (cheap generation check — one indexed COUNT
    query). Thread-safe via an RLock around build/lookup.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._index: dict[str, set[str]] = {}
        self._fact_entities: dict[str, list[str]] = {}
        self._built_for: tuple[str | None, int] | None = None

    # ------------------------------------------------------------------ build
    def _ensure(self, user_id: str | None) -> None:
        n = self._store.count_facts(user_id=user_id, active_only=True)
        key = (user_id, n)
        with self._lock:
            if self._built_for == key and self._index:
                return
            facts = self._store.query_facts(user_id=user_id, active=True,
                                            include_quarantined=False)
            index: dict[str, set[str]] = {}
            fact_entities: dict[str, list[str]] = {}
            for f in facts:
                ents = _entity_tokens(f.subject, f.value)
                fact_entities[f.id] = ents
                for e in ents:
                    index.setdefault(e, set()).add(f.id)
            self._index = index
            self._fact_entities = fact_entities
            self._built_for = key

    # ------------------------------------------------------------------ query
    def query_tokens(self, query: str) -> list[str]:
        """Query tokens that anchor the walk (must exist in the index)."""
        toks: list[str] = []
        for piece in (query or "").replace(",", " ").replace("?", " ") \
                .replace(".", " ").split():
            piece = piece.strip("'\"!?;:()[]").lower()
            if (len(piece) >= _MIN_TOKEN_LEN
                    and piece not in _STOPWORDS
                    and piece in self._index
                    and piece not in toks):
                toks.append(piece)
        return toks

    def walk(self, query: str, user_id: str | None,
             max_hops: int = 2, limit: int = 64,
             scope: frozenset[str] | set[str] | None = None
             ) -> tuple[dict[str, float], dict]:
        """Bounded BFS from the query's entity tokens.

        Returns (fact_id → raw score, stats). Scores:
          hop 1: +1.0 per DISTINCT query token indexing the fact
          hop 2: +0.5 per distinct hop-1 entity indexing the fact
        Scores are NOT normalized here — the caller scales by its own
        config weight. Facts outside ``scope`` (when given) are
        dropped.
        """
        self._ensure(user_id)
        stats = {"n_query_tokens": 0, "hop1": 0, "hop2": 0}
        with self._lock:
            anchors = self.query_tokens(query)
            stats["n_query_tokens"] = len(anchors)
            if not anchors:
                return {}, stats
            scores: dict[str, float] = {}
            # ---- hop 1: facts directly indexed by a query token ----
            hop1_entities: set[str] = set()
            for tok in anchors:
                for fid in self._index.get(tok, ()):
                    if scope is not None and fid not in scope:
                        continue
                    scores[fid] = scores.get(fid, 0.0) + 1.0
                    stats["hop1"] += 1
                    hop1_entities.update(self._fact_entities.get(fid, ()))
            # ---- hop 2: facts of hop-1 entities (anchors excluded) ----
            if max_hops >= 2:
                n_hop2 = 0
                for ent in sorted(hop1_entities):  # sorted → deterministic
                    if ent in anchors:
                        continue
                    for fid in sorted(self._index.get(ent, ())):
                        if scope is not None and fid not in scope:
                            continue
                        if fid in scores:
                            continue  # hop-1 facts keep their higher score
                        scores[fid] = 0.5
                        n_hop2 += 1
                        if n_hop2 >= limit:
                            break
                    if n_hop2 >= limit:
                        break
                stats["hop2"] = n_hop2
            return scores, stats


# ---------------------------------------------------------------------------
# Reader-facing facade (wired in bridge/reader.py behind
# Config.graph_recall_enabled)
# ---------------------------------------------------------------------------
_ACTIVE: dict[int, EntityGraphIndex] = {}


def graph_recall_boost(store: Any, query: str, user_id: str | None,
                       scope: frozenset[str] | set[str] | None = None,
                       max_hops: int = 2
                       ) -> tuple[dict[str, float], dict]:
    """Score boost map for one query, μ=0.

    Uses one process-wide index per store instance (id() keyed — the
    reader holds its store for its lifetime; the generation check
    inside rebuilds when facts change).
    """
    key = id(store)
    idx = _ACTIVE.get(key)
    if idx is None:
        idx = EntityGraphIndex(store)
        _ACTIVE[key] = idx
        # opportunistic cleanup: drop dead-store indexes
        if len(_ACTIVE) > 8:
            for k in list(_ACTIVE.keys()):
                if k != key and _ACTIVE[k]._built_for is None:
                    _ACTIVE.pop(k, None)
    return idx.walk(query, user_id, max_hops=max_hops, scope=scope)
