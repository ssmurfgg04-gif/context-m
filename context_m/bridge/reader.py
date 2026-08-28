"""Read path — deterministic neuro-symbolic query planner.

Query → intent parse (temporal / ordering / counting / supersession /
current-state / multi-hop / free recall) → parallel VSA palace search +
symbolic Trace queries → contradiction-chain & entity-hop expansion →
weighted fusion → context block with per-fact cryptographic provenance.

Every retrieval returns the full audit chain:
query → VSA match → symbolic dereference → source hash → source text.
No LLM calls at query time (edge-capable, offline).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from context_m import metrics
from context_m.bridge.dates import find_dates
from context_m.config import Config
from context_m.text.tokenizer import cap_sequences, content_words
from context_m.trace.fact import Fact
from context_m.trace.store import TraceStore
from context_m.util import normalize
from context_m.vsa.palace import MemoryPalace
from context_m.vsa.slb import SemanticLookasideBuffer


def _content_key(f: "Fact | None") -> tuple:
    """Deterministic ordering key for a fact. Fact ids are uuid4 (random
    per process); ties broken on id would make rankings — and therefore
    benchmark scores — vary across identical runs. Content never does."""
    if f is None:
        return ("~", "~", "~", "~")
    return (f.subject, f.relation, f.value, str(f.valid_from))


RELATION_HINTS = [
    # occupation idioms FIRST: "for a living" contains "living", which the
    # residence hint below would otherwise capture, drowning the `role`
    # fact under lives_in/moved_to noise.
    (re.compile(r"\b(for a living|occupation|profession|career|job title|"
                r"what does .{2,40}? do|works? as)\b", re.I),
     ["role", "works_at", "studied"]),
    (re.compile(r"\b(work\w*|employer|company|job)\b", re.I),
     ["works_at", "role"]),
    (re.compile(r"\b(live|lives|living|based|city|hometown|resid\w*|mov\w*)\b", re.I),
     ["lives_in", "moved_to"]),
    (re.compile(r"\b(prefer\w*|like\w*|favorite|favourite|taste)\b", re.I),
     ["prefers", "likes", "dislikes"]),
    (re.compile(r"\b(music|food|coffee|genre|playlist|meal|lunch|dinner|"
                r"drink|snack|editor|theme|picking|pick|choose|choosing)\b",
                re.I),
     ["prefers", "likes", "dislikes"]),
    (re.compile(r"\b(manag\w*|boss|report\w*|supervis\w*)\b", re.I),
     ["reports_to", "manages"]),
    (re.compile(r"\b(team|group|squad)\b", re.I),
     ["member_of", "manages", "team_uses", "uses"]),
    (re.compile(r"\b(skill\w*|know\w*|languag\w*|code|coding|program\w*|stack|tool\w*)\b", re.I),
     ["has_skill", "speaks", "team_uses", "uses"]),
    (re.compile(r"\b(birthday|born|age|years old)\b", re.I),
     ["birthday", "age"]),
    (re.compile(r"\b(sister|brother|mother|father|mom|dad|wife|husband|sibling|spouse|parent|family|daughter|son)\b", re.I),
     ["sibling", "parent", "spouse", "child"]),
    (re.compile(r"\b(project\w*|build\w*|ship\w*|launch\w*|develop\w*|releas\w*)\b", re.I),
     ["works_on", "completed", "event"]),
    (re.compile(r"\b(happen\w*|events?|did|done)\b", re.I), ["event"]),
    (re.compile(r"\b(stud\w*|school|degree|major|university|college|educat\w*)\b", re.I),
     ["studied", "studied_at"]),
    (re.compile(r"\b(pet|dog|cat|animal)\b", re.I), ["has_pet"]),
    (re.compile(r"\b(hobby|free time|weekend)\b", re.I), ["hobby"]),
    (re.compile(r"\b(nickname|alias|called|go by|full name|real name)\b", re.I),
     ["alias", "name"]),
    (re.compile(r"\b(goal|plan\w*|want to|hope)\b", re.I), ["goal"]),
    (re.compile(r"\b(instruction|always|never|respond|format|signature|guideline)\b", re.I),
     ["instruction"]),
]

CURRENT_MARKERS = re.compile(
    r"\b(current\w*|now|these days|nowadays|latest|today|present)\b", re.I)
SUPERSESSION_MARKERS = re.compile(
    r"\b(still|no longer|anymore|used to|former\w*|previous\w*|before|always|ever)\b", re.I)
ORDERING_MARKERS = re.compile(
    r"\b(which|what)\s+(?:one\s+)?happened\s+(?:first|before|earlier)\b"
    r"|\bbefore\s+.+?\s+or\b|\border\b|\bsequence\b|\bchronolog\w*\b"
    r"|\bfirst\s*:\s*.*\s+or\s+", re.I)
COUNT_MARKERS = re.compile(
    r"\bhow\s+many\s+(times|jobs|cities|companies|roles|moves|changes)\b"
    r"|\bhow\s+often\b", re.I)
LIST_MARKERS = re.compile(
    r"\b(?:list|name|enumerate|summar\w+)\b.*\ball\b|\ball\s+(?:the\s+)?\w+\s+"
    r"(?:that|which|she|he|they|i)\b|\blist\s+all\b", re.I)
MULTIHOP_MARKERS = re.compile(
    r"\b('s\b|of the|of my|of her|of his|of their)\b", re.I)


@dataclass
class QueryPlan:
    intent: str = "recall"
    entities: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    window_start: str | None = None
    window_end: str | None = None
    keywords: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    query: str
    intent: str
    facts: list[Fact]
    context_block: str
    provenance: dict
    timing: dict
    slb_hit: bool = False
    scores: dict = field(default_factory=dict)

    def memories(self) -> list[dict]:
        out = []
        for f in self.facts:
            out.append({
                "id": f.id,
                "memory": f"{f.subject} | {f.relation} | {f.value}",
                "score": self.scores.get(f.id, 0.0),
                "event": "ADD",
                "valid_from": f.valid_from,
                "valid_to": f.valid_to,
                "confidence": f.confidence,
                "hash": f.source_hash,
            })
        return out


class MemoryReader:
    def __init__(self, config: Config, store: TraceStore, palace: MemoryPalace,
                 prefetcher=None) -> None:
        self.cfg = config
        self.store = store
        self.palace = palace
        self.prefetcher = prefetcher
        self.slb = SemanticLookasideBuffer(
            config.slb_entries, config.slb_threshold, config.dims)
        self._scope_cache: dict[tuple, frozenset] = {}
        self._queries = 0
        # NSR-inspired swappable decoder (default = LLM prompt block,
        # preserving the original reader._context_block behavior).
        # Override via reader.with_decoder("rdf" | "datalog" | "json").
        from context_m.bridge.decoders import get_decoder
        self._decoder = get_decoder("llm_prompt")
        # Cross-encoder-style fact reranker (μ=0). Lazily imported so the
        # rest of the fabric is unaffected. Controlled by the
        # `enable_rerank` config knob (False by default — the bench must
        # explicitly enable it via "+rerank" config).
        self._reranker = None
        if getattr(config, "enable_rerank", False):
            try:
                from context_m.bridge.rerank import FactReranker
                self._reranker = FactReranker(
                    palace.embedder,
                    alpha=getattr(config, "rerank_alpha", 0.55),
                    beta=getattr(config, "rerank_beta", 0.45),
                    prf_alpha=getattr(config, "prf_alpha", 0.6),
                    prf_beta=getattr(config, "prf_beta", 0.4),
                    prf_topn=getattr(config, "prf_topn", 3))
            except Exception:  # noqa: BLE001
                self._reranker = None

    def with_decoder(self, name: str) -> "MemoryReader":
        """Swap the output decoder (NSR insight: same palace + Trace,
        different decoder). Returns self for chaining.

        Known decoders: 'llm_prompt' (default), 'rdf', 'datalog', 'json'.
        """
        from context_m.bridge.decoders import get_decoder
        self._decoder = get_decoder(name)
        return self

    # ------------------------------------------------------------- helpers
    def _scope_ids(self, user_id, agent_id, run_id, branch) -> frozenset:
        head = self.store.head(branch or self.store.current_branch()) or ""
        key = (user_id, agent_id, run_id, branch, head)
        hit = self._scope_cache.get(key)
        if hit is not None:
            return hit
        facts = self.store.query_facts(
            user_id=user_id, agent_id=None, run_id=run_id,
            active=True, include_quarantined=False)
        # InjecMEM scope sandbox:
        #  * user-scope query (agent_id=None) sees ONLY user-scoped facts;
        #    agent-written facts stay invisible until explicitly promoted.
        #  * agent query (agent_id=A) sees A's own facts PLUS the shared
        #    user scope — agents read the user's memory, never each
        #    other's.
        if agent_id is None:
            if getattr(self.cfg, "sandbox_enabled", True):
                facts = [f for f in facts if f.agent_id is None]
        else:
            facts = [f for f in facts if f.agent_id in (None, agent_id)]
        ids = frozenset(f.id for f in facts)
        if branch is not None:
            active = self.store.active_ids(branch)
            ids = ids & active
        if len(self._scope_cache) < 16:
            self._scope_cache[key] = ids
        return ids

    def invalidate_caches(self) -> None:
        self._scope_cache.clear()

    def _canonical_entities(self, query: str, user_id: str) -> list[str]:
        """cap-sequences + lexicon + alias resolution to canonical names."""
        cands: list[str] = []
        for seq in cap_sequences(query):
            cands.append(seq.strip().rstrip(".,!?;:"))
        # single capitalized words that are known entities
        for w in re.findall(r"\b[A-Z][a-z]{2,}\b", query):
            cands.append(w)
        name = self.store.kv_get(f"name:{user_id}")
        if re.search(r"\b(my|i|me)\b", query, re.I) and name:
            cands.append(name)

        lex_keys = set()
        import json
        raw = self.store.kv_get(f"lexicon:{user_id}", "[]")
        try:
            lex_keys = set(json.loads(raw))
        except Exception:
            pass
        canonical: list[str] = []
        seen = set()
        seen_out: set[str] = set()
        for c in cands:
            if not c or c.lower() in seen:
                continue
            seen.add(c.lower())
            resolved = None
            # alias resolution: (X, alias, c) → X
            for f in self.store.query_facts(relation="alias", value=c,
                                            user_id=user_id, active=True,
                                            limit=8):
                resolved = f.subject
                break
            if resolved is None and c in lex_keys:
                resolved = c
            if resolved is None and name and c.lower() == name.split()[0].lower():
                resolved = name
            if resolved and resolved.lower() not in seen_out:
                canonical.append(resolved)
                seen_out.add(resolved.lower())
        # order: prefer multi-word (full names) first
        canonical.sort(key=lambda s: -len(s))
        return canonical[:4]

    def _plan(self, query: str, user_id: str, ts: datetime | None) -> QueryPlan:
        plan = QueryPlan()
        for rx, rels in RELATION_HINTS:
            if rx.search(query):
                plan.relations.extend(rels)
        plan.relations = list(dict.fromkeys(plan.relations))[:5]
        plan.entities = self._canonical_entities(query, user_id)
        plan.keywords = content_words(query)

        if ORDERING_MARKERS.search(query):
            plan.intent = "ordering"
        elif COUNT_MARKERS.search(query):
            plan.intent = "count"
        elif LIST_MARKERS.search(query):
            plan.intent = "list"      # exhaustive set recall
        elif SUPERSESSION_MARKERS.search(query) or CURRENT_MARKERS.search(query):
            plan.intent = "current"
        dates = find_dates(query, ts or datetime.now(timezone.utc))
        if dates and plan.intent in ("recall", "current"):
            plan.intent = "temporal"
            plan.window_start = dates[0]["iso"]
            plan.window_end = dates[1]["iso"] if len(dates) > 1 else None
            if dates[0].get("granularity") == "year":
                y = dates[0]["iso"][:4]
                plan.window_start = f"{y}-01-01"
                plan.window_end = f"{y}-12-31"
            elif dates[0].get("granularity") in ("month", "ym_num"):
                # "in February 2025" — close the window at end-of-month;
                # an open-ended start would drag in every later event.
                y, mo = dates[0]["iso"][:4], int(dates[0]["iso"][5:7])
                _last = [31, 29 if (int(y) % 4 == 0 and (int(y) % 100 != 0
                            or int(y) % 400 == 0)) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
                plan.window_start = f"{y}-{mo:02d}-01"
                plan.window_end = f"{y}-{mo:02d}-{_last:02d}"
            elif dates[0].get("granularity") in ("day", "day_r", "iso_day"):
                plan.window_end = dates[0]["iso"]
        if re.search(r"\bbetween\s+.+\s+and\s+", query, re.I) and len(dates) >= 2:
            plan.window_start = min(d["iso"] for d in dates)
            plan.window_end = max(d["iso"] for d in dates)
        if re.search(r"\b(before)\b", query, re.I) and dates:
            plan.intent = "temporal"
            plan.window_end = dates[0]["iso"]
            plan.window_start = None
        if re.search(r"\b(after)\b", query, re.I) and dates:
            plan.intent = "temporal"
            plan.window_start = dates[0]["iso"]
            plan.window_end = None
        if MULTIHOP_MARKERS.search(query) and len(plan.relations) >= 2:
            plan.intent = "multihop" if plan.intent == "recall" else plan.intent
        return plan

    # ------------------------------------------------------------- search
    def search(self, query: str, *, user_id: str = "default",
               agent_id: str | None = None, run_id: str | None = None,
               k: int | None = None, ts: datetime | None = None,
               branch: str | None = None) -> RetrievalResult:
        t0 = time.perf_counter()
        k = k or self.cfg.top_k_default
        branch = branch or self.store.current_branch()
        metrics.bump_retrieval()
        self._queries += 1

        q_vec = self.palace.embedder.embed(query)
        plan = self._plan(query, user_id, ts)
        # intents that emit procedural notes (ORDERING: ..., temporal windows,
        # counts) cannot be served from the SLB: a cache hit would silently
        # drop the note and return the wrong fact set for the query shape.
        slb_ok = plan.intent not in ("ordering", "temporal", "count")
        scope_key = (user_id, agent_id, run_id, branch)
        cached = self.slb.lookup(q_vec, scope_key) if slb_ok else None
        if cached is not None:
            facts = self.store.get_facts([fid for fid, _ in cached])
            facts = [f for f in facts if f.is_active and not f.quarantined
                     and f.matches_scope(user_id, agent_id, run_id)
                     and (agent_id is not None or f.agent_id is None
                          or not getattr(self.cfg, "sandbox_enabled", True))]
            facts = facts[:k]
            t1 = time.perf_counter()
            self.slb.record_latency(True, t1 - t0)
            block = self._context_block(query, "recall", facts,
                                        {f.id: s for f, (fid, s) in zip(facts, cached)})
            return RetrievalResult(query, "recall", facts, block,
                                    self._provenance(query, facts),
                                    {"latency_ms": round((t1 - t0) * 1e3, 3),
                                     "slb": "hit"}, True,
                                    {f.id: s for f, (fid, s) in zip(facts, cached)})
        self.slb.misses += 1

        scope = self._scope_ids(user_id, agent_id, run_id, branch)

        # --- VSA path (neural recall) ------------------------------------
        # NOTE: an EMPTY scope is a real state (scope sandbox: the user
        # owns no visible facts, e.g. everything is agent-scoped). It must
        # filter to nothing — the old `if scope else None` fallback turned
        # it into an UNRESTRICTED search that leaked agent-scoped (and
        # cross-user) vectors into the result set.
        vsa_hits = self.palace.search(q_vec, max(k * self.cfg.search_k_mult, 24),
                                      candidate_ids=set(scope))
        vsa_scores = {fid: float(s) for fid, s in vsa_hits}

        # --- symbolic path -------------------------------------------------
        sym_facts, notes = self._symbolic_query(plan, user_id, agent_id, run_id,
                                                scope, k, query)

        # --- fusion ---------------------------------------------------------
        # 'mentioned' anchors are retrieval scaffolding, not answers: their
        # long snippets inflate lexical similarity, so ONLY their VSA
        # contribution is damped. Symbolic exacts always dominate.
        vsa_ids = list(vsa_scores.keys())
        fact_map = {f.id: f for f in self.store.get_facts(vsa_ids)}
        sym_map = {f.id: f for f, _ in sym_facts}
        fact_map.update({k: v for k, v in sym_map.items() if k not in fact_map})
        candidates: dict[str, float] = {}
        for fid, s in vsa_scores.items():
            f = fact_map.get(fid)
            rel = f.relation if f else None
            damp = 0.45 if rel == "mentioned" else 1.0
            candidates[fid] = candidates.get(fid, 0.0) + \
                self.cfg.fusion_vsa_weight * max(0.0, s) * damp
        for f, boost in sym_facts:
            hinted = f.relation in plan.relations
            b = boost + (0.2 if hinted else 0.0)
            candidates[f.id] = candidates.get(f.id, 0.0) + \
                self.cfg.fusion_symbolic_weight * b

        # prefetch boost (MBTB) — cache-warming heuristic ONLY for simple
        # recall/current intents: for precision intents (multihop,
        # temporal, ordering, count) the co-access boost reorders the
        # ranking away from graph-relevant evidence.
        prefetch_boosted = set()
        if (self.prefetcher is not None
                and plan.intent in ("recall", "current")):
            for fid, w in self.prefetcher.predict().items():
                if fid in candidates:
                    candidates[fid] += 0.05 * w
                    prefetch_boosted.add(fid)

        # expansion: contradiction chains + temporal neighbors + hops.
        # Tie-break on fact CONTENT, never on ids: ids are uuid4 (random per
        # process), so id-based ties would shuffle results across runs.
        _f0 = {f.id: f for f in self.store.get_facts(list(candidates))}
        top = sorted(candidates.items(),
                     key=lambda kv: (-kv[1], _content_key(_f0.get(kv[0]))))[:k]
        top_ids = [fid for fid, _ in top]
        extra = self._expand(top_ids, plan, scope, user_id, query)
        for fid, w in extra.items():
            candidates.setdefault(fid, 0.0)
            candidates[fid] += w

        # Personalized PageRank diffusion (HippoRAG 2 lineage): graph
        # activation from the current top set spreads to multi-hop
        # evidence — the entity-hop expansion above is its depth-2
        # approximation; PPR is the full diffusion.
        if self.cfg.ppr_enabled and plan.intent in ("multihop", "recall"):
            ppr_ids = list(candidates.keys())[: self.cfg.ppr_graph_size]
            ppr_facts = self.store.get_facts(ppr_ids)
            if len(ppr_facts) >= 2:
                from context_m.bridge.ppr import ppr_boost
                seed_ids = top_ids[: self.cfg.ppr_seeds]
                edges = self.store.edges_of_many(ppr_ids, "CONTRADICTS")
                boosts = ppr_boost(ppr_facts, seed_ids, edges,
                                   damping=self.cfg.ppr_damping,
                                   iters=self.cfg.ppr_iters)
                for fid, b in boosts.items():
                    candidates[fid] += self.cfg.ppr_weight * b

        _f1 = {f.id: f for f in self.store.get_facts(list(candidates))}
        ranked_ids = self._diversify(
            sorted(candidates,
                   key=lambda fid: (-candidates[fid], _content_key(_f1.get(fid)))), k)
        facts = self.store.get_facts(ranked_ids)
        allow_inactive = plan.intent in ("temporal", "current", "count")
        facts = [f for f in facts if not f.quarantined
                 and (f.is_active or allow_inactive)]
        facts.sort(key=lambda f: ranked_ids.index(f.id))

        # --- cross-encoder rerank (μ=0) -------------------------------------
        # If enabled, re-score top-k by embedding each fact's natural-
        # language rendering and computing cosine sim to the query. The
        # chunk vectors in the palace are long and fact-dense — a fact-
        # level embedding is focused and lifts precision@k by 10-20pp on
        # MS-MARCO-style benchmarks (cross-encoder reranking, web search
        # 2026-08). The candidate pool is expanded to 3*k for the rerank
        # pass so we have a deeper top-N to draw from. PRF (Rocchio)
        # shifts the query embedding toward the mean of the top-3 fact
        # NL embeddings — a 2-5pp lift on TREC.
        rerank_used = False
        if (self._reranker is not None
                and plan.intent in ("recall", "current", "multihop")
                and len(facts) >= 2):
            # expand the candidate pool back to the wider fusion set so
            # the rerank can find facts the diversifier dropped
            pool_ids = [fid for fid, _ in
                        sorted(candidates.items(),
                               key=lambda kv: (-kv[1],
                                               _content_key(_f1.get(kv[0]))))
                        [:max(k * 3, 15)]]
            pool_facts = [f for f in self.store.get_facts(pool_ids)
                          if (not f.quarantined
                              and (f.is_active or allow_inactive))]
            pool_scores = {f.id: candidates.get(f.id, 0.0) for f in pool_facts}
            reranked, new_scores = self._reranker.rerank(
                q_vec, pool_facts, pool_scores, top_k=k, enable_prf=True)
            if reranked:
                facts = reranked
                ranked_ids = [f.id for f in facts]
                # patch candidates so _context_block and SLB see rerank scores
                candidates.update(new_scores)
                rerank_used = True

        if self.prefetcher is not None and facts:
            self.prefetcher.observe([f.id for f in facts[:6]])

        self.store.bump_access([f.id for f in facts])
        t1 = time.perf_counter()
        self.slb.record_latency(False, t1 - t0)
        self.slb.store(q_vec, [(f.id, candidates.get(f.id, 0.0)) for f in facts],
                       query=query, scope=scope_key if slb_ok else ("__no_cache__",))

        block = self._context_block(query, plan.intent, facts, candidates,
                                    notes)
        return RetrievalResult(
            query, plan.intent, facts, block,
            self._provenance(query, facts, vsa_scores),
            {"latency_ms": round((t1 - t0) * 1e3, 3), "slb": "miss",
             "prefetch_boosted": len(prefetch_boosted),
             "vsa_candidates": len(vsa_scores),
             "symbolic_candidates": len(sym_facts),
             "rerank": rerank_used},
            False, {f.id: round(candidates.get(f.id, 0.0), 4) for f in facts})

    # ------------------------------------------------------------- symbolic
    def _symbolic_query(self, plan: QueryPlan, user_id, agent_id, run_id,
                        scope, k, query):
        out: list[tuple[Fact, float]] = []
        notes: list[str] = []
        add = lambda f, w: out.append((f, w))  # noqa: E731

        def in_scope(f: Fact) -> bool:
            # scope is always a frozenset; an EMPTY scope means "nothing
            # visible" (scope sandbox) and must not fall through to
            # unfiltered access the way a falsy check would.
            return (f.id in scope) and f.is_active and not f.quarantined

        if plan.entities:
            for ent in plan.entities[:2]:
                for f in self.store.facts_about(ent, user_id=user_id):
                    if in_scope(f):
                        boost = 1.0 if f.relation in plan.relations else 0.7
                        add(f, boost)

        # ordering intent: resolve two events, emit ORDERING note
        if plan.intent == "ordering":
            notes.extend(self._ordering_notes(plan, user_id, query, in_scope))

        # count intent
        if plan.intent == "count":
            for ent in plan.entities[:1]:
                for rel in plan.relations or ["works_at"]:
                    hist = self.store.history_of(ent, rel, user_id=user_id)
                    n = max(0, len({f.value for f in hist}) - 1)
                    if hist:
                        notes.append(f"COUNT: {ent} has {len({f.value for f in hist})} "
                                     f"recorded value(s) for '{rel}' "
                                     f"({n} change(s))")
                    for f in hist:
                        if in_scope(f) or not f.is_active:
                            add(f, 0.6)

        # supersession / current intent: pull full contradiction chains
        if plan.intent in ("current", "temporal", "recall"):
            pulled = {f.id for f, _ in out}
            for ent in plan.entities[:2]:
                for rel in (plan.relations or [])[:3]:
                    for f in self.store.history_of(ent, rel, user_id=user_id)[:6]:
                        if f.id not in pulled and f.id in scope:
                            if f.value in plan.entities:
                                b = 0.9
                            elif f.is_active:
                                b = 0.55
                            else:
                                b = 0.45
                            add(f, b)
                            pulled.add(f.id)

        # temporal window
        if plan.window_start or plan.window_end:
            for f in self.store.temporal_window(
                    plan.window_start, plan.window_end, user_id=user_id,
                    active=False):
                if (not f.quarantined and f.id in scope
                        and (not plan.entities or
                             any(e in (f.subject, f.value)
                                 for e in plan.entities) or
                             f.relation in plan.relations)):
                    hinted = f.relation in plan.relations
                    # tier 1: the fact BEGAN inside the window — this is what
                    # "what happened in <window>" asks for. tier 2: still-valid
                    # background state that merely overlaps the window.
                    vf = f.valid_from or ""
                    began_in = ((plan.window_start is None
                                 or vf >= plan.window_start)
                                and (plan.window_end is None
                                     or vf <= plan.window_end))
                    if began_in:
                        b = (1.0 if hinted else 0.9) if f.is_active \
                            else (0.9 if hinted else 0.75)
                    else:
                        b = (0.6 if hinted else 0.5) if f.is_active \
                            else (0.5 if hinted else 0.4)
                    add(f, b)

        return out, notes

    # ------------------------------------------------------------- ordering
    def _ordering_notes(self, plan, user_id, query, in_scope):
        notes = []
        # find two event-ish keywords (quoted or capitalized)
        parts = re.split(r"\s+or\s+|\?|,|;", query)
        kw_sets = [set(content_words(p)) for p in parts if p.strip()]
        all_events = self.store.query_facts(relation="event", user_id=user_id,
                                            active=True, order="valid_from")
        events: list[Fact] = []
        for ks in kw_sets:
            best, best_ov = None, 0
            for f in all_events:
                words = set(content_words(f.value)) | {f.value.lower()}
                ov = len(ks & words)
                if ov > best_ov:
                    best, best_ov = f, ov
            need = 2 if len(ks) >= 2 else 1
            if best is not None and best_ov >= need and best not in events:
                events.append(best)
        if len(events) < 2:
            # fall back to any two dated facts around entities
            evs = [f for f in self.store.query_facts(
                relation="event", user_id=user_id, active=True,
                order="valid_from")][:50]
            events = evs[:2] if len(evs) >= 2 else events
        if len(events) >= 2:
            a, b = sorted(events[:2], key=lambda f: f.valid_from)
            notes.append(f"ORDERING: {a.value} ({a.valid_from}) happened before "
                         f"{b.value} ({b.valid_from})")
            self._pending_ordering = getattr(self, "_pending_ordering", [])
            self._pending_ordering.extend([a, b])
        return notes

    def _diversify(self, ranked_ids: list[str], k: int,
                   per_relation: int = 4) -> list[str]:
        """Cap slots per relation so one relation cannot flood the block."""
        if len(ranked_ids) <= k:
            return ranked_ids
        facts = self.store.get_facts(ranked_ids[: max(k * 3, 48)])
        rel_of = {f.id: f.relation for f in facts}
        counts: dict[str, int] = {}
        first, rest = [], []
        for fid in ranked_ids:
            rel = rel_of.get(fid, "?")
            if counts.get(rel, 0) < per_relation:
                counts[rel] = counts.get(rel, 0) + 1
                first.append(fid)
            else:
                rest.append(fid)
        out = (first + rest)[:k]
        return out

    # ------------------------------------------------------------- expand
    def _expand(self, top_ids: list[str], plan: QueryPlan, scope, user_id,
                query: str) -> dict[str, float]:
        extra: dict[str, float] = {}
        facts = self.store.get_facts(top_ids[:4])
        for f in facts:
            for e in self.store.edges_of(f.id, "CONTRADICTS", "out"):
                extra.setdefault(e["dst"], 0.35)
            # same subject-relation history
            for h in self.store.history_of(f.subject, f.relation,
                                           user_id=user_id)[:4]:
                if h.id not in top_ids:
                    extra.setdefault(h.id, 0.3)
        # entity-hop expansion (multi-hop associative recall, 2 rounds
        # when the query is compositional: manager-of-X-team-uses-Y)
        if plan.intent in ("multihop", "recall") and facts:
            rounds = 2 if plan.intent == "multihop" else 1
            frontier = facts[:4]
            seen = set(top_ids)
            for _rd in range(rounds):
                nxt = []
                for f in frontier[:6]:
                    for g in self.store.facts_about(f.value, user_id=user_id)[:6]:
                        if g.id not in seen and g.is_active:
                            # value-hop = chain completion (X → value → Z):
                            # the semantic shape of every multi-hop question.
                            w = (0.7 if _rd == 0 and plan.intent == "multihop"
                                 else (0.5 if _rd == 0 else 0.35))
                            extra.setdefault(g.id, w)
                            seen.add(g.id)
                            nxt.append(g)
                    for g in self.store.facts_about(f.subject, user_id=user_id)[:8]:
                        if g.id not in seen and g.is_active:
                            extra.setdefault(g.id, 0.3)
                            seen.add(g.id)
                            nxt.append(g)
                frontier = nxt
                if len(extra) > 60:
                    break
        # ordering events force-included
        for f in getattr(self, "_pending_ordering", []):
            extra.setdefault(f.id, 0.9)
        self._pending_ordering = []
        if scope:
            extra = {fid: w for fid, w in extra.items() if fid in scope}
        return extra

    # ------------------------------------------------------------- format
    def _context_block(self, query: str, intent: str, facts: list[Fact],
                       scores: dict, notes: list[str] | None = None) -> str:
        # Route through the swappable decoder (default = LLMPromptDecoder
        # preserves the original "[Memory — Known facts]" block format).
        # Callers can swap via reader.with_decoder("rdf" | "datalog" | "json")
        # to serve non-LLM workloads from the SAME retrieval pipeline.
        return self._decoder.render(
            query=query, intent=intent, facts=facts,
            scores=scores or {}, notes=notes, store=self.store)

    def _provenance(self, query: str, facts: list[Fact],
                    vsa_scores: dict | None = None) -> dict:
        chain = []
        for f in facts[:12]:
            chunk = self.store.get_chunk(f.source_id) if f.source_id else None
            chain.append({
                "fact_id": f.id,
                "triple": f.display(),
                "vsa_score": round(vsa_scores.get(f.id, 0.0), 4)
                if vsa_scores else None,
                "source_hash": f.source_hash,
                "source_verified": bool(chunk and chunk["hash"] == f.source_hash),
                "source_text": (chunk["text"][:160] if chunk else None),
                "valid_from": f.valid_from, "valid_to": f.valid_to,
                "tx_from": f.tx_from,
                "confidence": f.confidence,
            })
        return {"query": query, "chain": chain,
                "verification": all(c["source_verified"] for c in chain)}
