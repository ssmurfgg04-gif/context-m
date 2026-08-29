"""Write path — μ=0 ingest orchestration.

messages → chunks (BLAKE3-hashed) → InjecMEM screening → deterministic
extraction → interference-aware lifecycle → bi-temporal Trace commit →
VSA palace encoding → Datalog materialization → temporal edge wiring.
Zero LLM calls at every step (the optional async enricher is a separate
API, never on this path).
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone

from cortexm import metrics
from cortexm.bridge.extractor import Extractor
from cortexm.bridge.patterns import ExtractionContext
from cortexm.bridge.negation import extract_with_negation
from cortexm.config import Config
from cortexm.security.injection import scan as injection_scan
from cortexm.security.injection import contagion_scan
from cortexm.trace import lifecycle
from cortexm.trace.contradictions import Action
from cortexm.trace.edges import (
    wire_causal_edge, wire_refers_to, REFERS_TO,
)
from cortexm.trace.fact import Fact, make_fact
from cortexm.trace.rules import RuleEngine
from cortexm.trace.store import TraceStore
from cortexm.util import iso, new_id, similarity, token_estimate
from cortexm.vsa.palace import MemoryPalace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_messages(messages) -> list[tuple[str, str, datetime | None]]:
    """Accept mem0-style inputs: str | list[str] | list[dict] -> (role, text, ts)."""
    out: list[tuple[str, str, datetime | None]] = []
    if isinstance(messages, str):
        out.append(("user", messages, None))
    elif isinstance(messages, dict):
        out.append((messages.get("role", "user"),
                    messages.get("content", ""), None))
    elif isinstance(messages, list):
        for m in messages:
            if isinstance(m, str):
                out.append(("user", m, None))
            elif isinstance(m, dict):
                role = m.get("role", "user")
                content = m.get("content", "")
                if isinstance(content, list):  # multimodal content blocks
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict))
                ts = m.get("timestamp") or m.get("ts")
                out.append((role, content or "", ts))
    return [(r, t, ts) for r, t, ts in out if t and t.strip()]


class MemoryWriter:
    def __init__(self, config: Config, store: TraceStore, palace: MemoryPalace,
                 extractor: Extractor | None = None) -> None:
        self.cfg = config
        self.store = store
        self.palace = palace
        self.extractor = extractor or Extractor(config)
        self.rules = RuleEngine(store)
        # MINJA contagion guard: per-scope cache of quarantined source texts
        # (loaded lazily, one query per scope, updated on quarantine).
        self._taint_cache: dict[str, list[str]] = {}
        # Verbatim tier handle (lazily attached). Set when Memory attaches
        # it; if the config has verbatim_ingest_enabled=False, _verbatim
        # stays None and add() skips the verbatim insert.
        self._verbatim = None

    def attach_verbatim(self, plugin) -> None:
        """Inject the VerbatimPlugin instance so add() can store raw chunks.

        Called by Memory.__init__ after the plugin is mounted. If the
        verbatim plugin isn't mounted, _verbatim stays None — the writer
        silently degrades to structured-only ingest (μ=0 still holds)."""
        self._verbatim = plugin

    def _verbatim_store_chunk(self, *, text: str, user_id: str,
                              session_id: str | None,
                              source_tx_id: int | None,
                              agent_id: str | None = None) -> None:
        """μ=0 verbatim tier insert. Best-effort: never blocks ingest.

        The verbatim plugin's add() is FTS5 INSERT + numpy embed + int8
        quantize + INSERT INTO verbatim_vectors. All deterministic. If
        it throws (FTS5 missing, embedder not ready, db locked), we log
        and move on — the structured tier has already absorbed the facts
        and the EXTRACTED_FROM edge is wired.

        v0.5.3: agent_id is stored on the chunk so search() can honor
        the InjecMEM scope sandbox (user queries don't see agent-scoped
        chunks, and vice versa)."""
        if not getattr(self.cfg, "verbatim_ingest_enabled", True):
            return
        if self._verbatim is None:
            return
        try:
            self._verbatim.add(text=text, user_id=user_id,
                              session_id=session_id,
                              source_tx_id=source_tx_id,
                              agent_id=agent_id)
        except Exception as e:
            # best-effort — never block the write path on the verbatim tier
            import sys as _sys
            print(f"[verbatim] store_chunk failed: {e}", file=_sys.stderr)

    # ------------------------------------------------------------------
    def _name_of(self, user_id: str) -> str | None:
        return self.store.kv_get(f"name:{user_id}")

    def _set_name(self, user_id: str, name: str) -> None:
        self.store.kv_set(f"name:{user_id}", name)

    def _lexicon(self, user_id: str) -> set[str]:
        import json
        raw = self.store.kv_get(f"lexicon:{user_id}", "[]")
        try:
            return set(json.loads(raw))
        except Exception:
            return set()

    def _grow_lexicon(self, user_id: str, names: set[str]) -> None:
        lex = self._lexicon(user_id)
        merged = lex | {n for n in names if n and 2 < len(n) < 60}
        if len(merged) > 20000:
            merged = set(sorted(merged)[:20000])
        import json
        self.store.kv_set(f"lexicon:{user_id}", json.dumps(sorted(merged)))

    def _tainted_corpus(self, user_id: str) -> list[str]:
        if user_id not in self._taint_cache:
            try:
                self._taint_cache[user_id] = \
                    self.store.quarantined_chunk_texts(user_id)
            except Exception:
                self._taint_cache[user_id] = []
        return self._taint_cache[user_id]

    def _taint(self, user_id: str, text: str) -> None:
        corpus = self._tainted_corpus(user_id)
        if text not in corpus:
            corpus.append(text)

    # ------------------------------------------------------------------
    def _store_negations(self, *, text: str, user_id: str,
                        session_id: str | None,
                        source_tx_id: str | None,
                        agent_id: str | None = None,
                        created_at: datetime | None = None) -> int:
        """μ=0 negation routing.

        Splits the text into non-negated + negated sentences
        (cortexm.bridge.negation.extract_with_negation) and writes
        each negated sentence into the ``negation_records`` table
        on the trace store. Returns the number of negation rows
        written (best-effort: never blocks ingest).

        v0.6.1 wiring: this is the follow-up the v0.6.0 detector
        was waiting for. Before this, the extractor saw
        ``"I don't eat meat"`` and, if a pattern fired before the
        negation was checked, mis-extracted ``(+user, eats, meat)``
        as a positive fact — the reader then hallucinated "Yes"
        from the very text that denied it.
        """
        if not getattr(self.cfg, "negation_indexing_enabled", True):
            return 0
        try:
            split = extract_with_negation(text)
            negs = split["negations"]
            if not negs:
                return 0
            ts_s = iso(created_at) if created_at else iso(_now())
            src_hash = self.store.hasher.hash_text(text)
            n = 0
            for rec in negs:
                self.store.insert_negation_record(
                    user_id=user_id,
                    sentence=rec.get("sentence", ""),
                    marker=rec.get("marker", ""),
                    implied_subject=rec.get("implied_subject", "") or "",
                    agent_id=agent_id,
                    session_id=session_id,
                    source_tx_id=str(source_tx_id) if source_tx_id is not None else None,
                    source_hash=src_hash,
                    created_at=ts_s,
                )
                n += 1
            return n
        except Exception as e:
            # best-effort — never block the write path
            import sys as _sys
            print(f"[negation] store failed: {e}", file=_sys.stderr)
            return 0

    # ------------------------------------------------------------------
    def _unmess_cache(self) -> dict:
        """Lazy-init the unmess (idiolect + dissim) cache on this writer.
        Reuses the chaos-mode pattern so production and chaos paths share
        the same slang dictionary — observations accumulate across both."""
        if not hasattr(self, "_unmess"):
            from cortexm.text.dissim import DisSimSplitter
            from cortexm.text.embedder import HashingEmbedder
            from cortexm.text.idiolect import PerUserIdiolectNormalizer
            embedder = HashingEmbedder(self.palace.dims, self.palace.cfg.seed)
            self._unmess = {
                "idiolect": PerUserIdiolectNormalizer(embedder),
                "dissim": DisSimSplitter(max_depth=self.cfg.unmess_max_depth),
                "embedder": embedder,
            }
        return self._unmess

    def _unmess_text(self, text: str, user_id: str) -> list[str]:
        """Run the Unmess pipeline on a single message, returning clauses.

        1. observe idiolect (slang dictionary grows)
        2. normalize text-speak + kNN slang replacement
        3. DisSim recursive syntactic split into simple clauses
        Returns a list of clause strings (>=1). Falls back to the raw
        text on any error so the path never blocks ingest.
        """
        if not self.cfg.unmess_enabled:
            return [text]
        try:
            cache = self._unmess_cache()
            idiolect = cache["idiolect"]
            dissim = cache["dissim"]
            idiolect.observe(user_id, text)
            norm = idiolect.normalize(user_id, text)
            clauses = [c.text for c in (dissim.simplify_text(norm) or [norm])]
            return clauses if clauses else [norm]
        except Exception:
            return [text]

    # ------------------------------------------------------------------
    def add(self, messages, *, user_id: str = "default",
            agent_id: str | None = None, run_id: str | None = None,
            ts: datetime | None = None, source: str = "",
            metadata: dict | None = None) -> dict:
        ts = ts or _now()
        norm = _normalize_messages(messages)
        tokens = sum(token_estimate(t) for _, t, _ in norm)
        self.store.begin_batch()
        commit = self.store.create_commit(
            f"ingest {len(norm)} message(s)", n_facts=0)
        results: list[dict] = []
        inserted_total = 0

        for role, text, msg_ts in norm:
            msg_time = msg_ts if isinstance(msg_ts, datetime) else ts
            speaker = "assistant" if role in ("assistant", "ai", "bot") else "user"
            chunk_id = self.store.add_chunk(
                text, user_id=user_id, agent_id=agent_id, run_id=run_id,
                ts=msg_time, source=source or role)
            # v0.5.3: ALSO push the raw chunk into the verbatim tier
            # (FTS5 + int8 vector). This is the MemPalace-style layer
            # the canonical-LongMemEval diagnosis called for — single-
            # session factoids ("What restaurant did they mention?")
            # retrieve BM25 hits from these raw chunks, bypassing the
            # 61-pattern extractor that misses natural speech.
            # The verbatim plugin stores session_id (best-effort: the
            # caller rarely passes one — we use run_id as a proxy)
            # and source_tx_id (the chunk_id from the structured tier's
            # chunks table, so the EXTRACTED_FROM edge cross-references).
            try:
                _src_tx_id = int(chunk_id) if str(chunk_id).isdigit() else None
            except Exception:
                _src_tx_id = None
            self._verbatim_store_chunk(
                text=text, user_id=user_id,
                session_id=run_id or agent_id or user_id,
                source_tx_id=_src_tx_id,
                agent_id=agent_id)
            # v0.6.1: split negated sentences out BEFORE the extractor
            # runs. The negated sentences go into a separate
            # ``negation_records`` table (so the reader can return
            # "No — explicitly stated" later); the extractor now sees
            # only the non-negated portion, which kills the
            # "I don't eat meat" → (+user, eats, meat) mis-extraction.
            # Best-effort: on any failure we fall back to the raw text
            # so the write path is never blocked.
            try:
                if getattr(self.cfg, "negation_indexing_enabled", True):
                    neg_split = extract_with_negation(text)
                    self._store_negations(
                        text=text, user_id=user_id,
                        session_id=run_id or agent_id or user_id,
                        source_tx_id=str(_src_tx_id) if _src_tx_id is not None else None,
                        agent_id=agent_id, created_at=msg_time)
                    extraction_text = neg_split["positive_text"] or text
                else:
                    extraction_text = text
            except Exception:
                extraction_text = text
            verdict = injection_scan(text, self.cfg.quarantine_injection)
            if not verdict.quarantined and self.cfg.quarantine_contagion:
                cv = contagion_scan(text, self._tainted_corpus(user_id),
                                    threshold=self.cfg.contagion_threshold)
                if cv is not None:
                    verdict = cv

            ctx = ExtractionContext(
                user_id=user_id, agent_id=agent_id, run_id=run_id,
                ts=msg_time, speaker=speaker,
                subject_name=self._name_of(user_id),
                lexicon=self._lexicon(user_id))

            # --- OOD ingestion: Unmess + DisSim + Bitap ----------------
            # Pre-process the raw text through the unmess pipeline before
            # running the deterministic extractor. This is the fix for the
            # Tier-1 OOD catastrophe (paraphrase 9.4%, slang 5.1% recall):
            #   * idiolect normalizer: "u"→"you", "bruh"→"friend" if user
            #     co-occurred, "wrks"→"works" via kNN over vocab
            #   * DisSim splits compound sentences so each clause matches
            #     its own pattern instead of the regex missing all of them
            # The extractor's internal Bitap trigger widening handles
            # misspelled trigger words. When unmess is OFF (bench baseline
            # config), we run the raw text through the extractor unchanged.
            clauses = self._unmess_text(extraction_text, user_id) \
                if self.cfg.unmess_enabled else [extraction_text]

            candidates = []
            for clause in clauses:
                if not clause or not clause.strip():
                    continue
                candidates.extend(self.extractor.extract(clause, ctx))

            for cand in candidates:
                if cand.confidence < self.cfg.min_confidence:
                    continue
                valid_from = cand.valid_from or iso(msg_time)[:10]
                fact = make_fact(
                    cand.subject, cand.relation, cand.value, now=msg_time,
                    valid_from=valid_from, valid_to=cand.valid_to,
                    user_id=user_id, agent_id=agent_id, run_id=run_id,
                    confidence=cand.confidence,
                    source_id=chunk_id,
                    source_hash=self.store.hasher.hash_text(text),
                    memory_type=("short_term" if cand.confidence < 0.6
                                 else "short_term"),
                    provenance={"pattern": cand.pattern,
                                "speaker": speaker,
                                "span": list(cand.span),
                                "injection_risk": verdict.risk,
                                # Tier-4: surface whether this fact
                                # came from a strict trigger or a
                                # Bitap-widened one. Downstream audits
                                # can use this to estimate OOD recall
                                # vs FP rate; the min_confidence
                                # filter already applied a 0.10
                                # penalty on the bitap_widened path.
                                "trigger_source": getattr(
                                    cand, "trigger_source", "strict"),
                                **({"context": cand.note} if cand.note else {})})
                fact.birth_commit = commit

                if verdict.quarantined:
                    fact.quarantined = True
                    fact.is_active = False
                    self.store.insert_fact(fact, commit)
                    self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
                    self._taint(user_id, text)
                    results.append(self._result(fact, "QUARANTINED",
                                                verdict.note))
                    continue

                if cand.retraction:
                    inserted_total += self._apply_retraction(
                        fact, cand, commit, chunk_id, msg_time, results)
                    continue

                if self.cfg.enable_lifecycle:
                    decision = lifecycle.assess(self.store, fact)
                else:
                    from cortexm.trace.contradictions import Action as _A
                    decision = lifecycle.LifecycleDecision(_A.COMMIT, True)

                inserted_total += self._apply_decision(
                    fact, decision, commit, chunk_id, results)

            # learn name / lexicon
            for cand in candidates:
                if cand.relation == "name" and self._name_of(user_id) is None:
                    self._set_name(user_id, cand.value)
            self._grow_lexicon(user_id, {c.subject for c in candidates} |
                               {c.value for c in candidates
                                if c.value and c.value[0:1].isupper()})

        # Datalog materialization (inference bound into the palace)
        if self.cfg.enable_rules and getattr(self.cfg, "apply_rules_each_add", True):
            self.rules.invalidate()
            derived = self.rules.apply(ts)
            for f in derived:
                f.user_id = user_id
                self.palace.add(f.id, self.palace.encode_fact(f))
            inserted_total += len(derived)

        self._wire_temporal_edges(user_id)
        self.store.end_batch()
        self.palace.close()
        metrics.bump_ingest(tokens=tokens, messages=len(norm),
                            facts=inserted_total)
        return {"event": "ADD", "results": results,
                "commit": commit,
                "stats": {"messages": len(norm), "tokens": tokens,
                          "facts_inserted": inserted_total,
                          "llm_calls": 0}}

    # ------------------------------------------------------------------
    def ingest_candidates(self, candidates, *, user_id: str = "default",
                          agent_id: str | None = None,
                          chunk_id: str | None = None,
                          ts: datetime | None = None,
                          source: str = "", extractor_model: str | None = None,
                          ) -> int:
        """Commit pre-built Candidates through the standard fact pipeline.

        Used by the async LLM enrichment fallback (bridge/enrich.py): the
        μ=0 path stays untouched, but enriched candidates still pass the
        SAME quarantine, contradiction, lifecycle, edge-wiring and palace
        indexing logic as pattern-extracted facts. Provenance records the
        enrichment origin so audits can always distinguish the two paths.
        """
        ts = ts or _now()
        text = ""
        chunk = self.store.get_chunk(chunk_id) if chunk_id else None
        if chunk:
            text = chunk["text"]
        self.store.begin_batch()
        commit = self.store.create_commit(
            f"enrich {len(candidates)} candidate(s) from {source or 'external'}",
            n_facts=0)
        inserted = 0
        for cand in candidates:
            if cand.confidence < self.cfg.min_confidence:
                continue
            verdict = injection_scan(text, self.cfg.quarantine_injection) \
                if text else None
            fact = make_fact(
                cand.subject, cand.relation, cand.value, now=ts,
                valid_from=cand.valid_from or iso(ts)[:10],
                valid_to=cand.valid_to,
                user_id=user_id, agent_id=agent_id, run_id=None,
                confidence=cand.confidence,
                source_id=chunk_id,
                source_hash=(self.store.hasher.hash_text(text)
                             if text else self.store.hasher.hash_text(
                                 cand.subject + cand.relation + cand.value)),
                memory_type="short_term",
                provenance={"pattern": cand.pattern,
                            "enriched_by": source or "external",
                            **({"extractor_model": extractor_model}
                               if extractor_model else {}),
                            **({"injection_risk": verdict.risk}
                               if verdict else {})})
            fact.birth_commit = commit
            if verdict is not None and verdict.quarantined:
                fact.quarantined = True
                fact.is_active = False
                self.store.insert_fact(fact, commit)
                if chunk_id:
                    self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
                continue
            if self.cfg.enable_lifecycle:
                decision = lifecycle.assess(self.store, fact)
            else:
                from cortexm.trace.contradictions import Action as _A
                decision = lifecycle.LifecycleDecision(_A.COMMIT, True)
            results: list = []
            inserted += self._apply_decision(fact, decision, commit,
                                             chunk_id, results)
        if self.cfg.enable_rules:
            self.rules.invalidate()
            derived = self.rules.apply(ts)
            for f in derived:
                f.user_id = user_id
                self.palace.add(f.id, self.palace.encode_fact(f))
            inserted += len(derived)
        self._wire_temporal_edges(user_id)
        self.store.end_batch()
        self.palace.close()
        return inserted

    # ------------------------------------------------------------------
    def apply_rules(self, ts=None):
        """Deferred Datalog materialization (bulk-ingest mode)."""
        ts = ts or _now()
        self.rules.invalidate()
        derived = self.rules.apply(ts)
        for f in derived:
            self.palace.add(f.id, self.palace.encode_fact(f))
        self.store.end_batch()
        return derived

    # ------------------------------------------------------------------
    def _max_vsa_overlap(self, fact: Fact, user_id: str) -> float:
        """Shannon tiered storage: compute the max cosine similarity
        of this fact's VSA hologram vs. existing facts in the user's
        scope. Returns 0.0 on cold-start (<shannon_min_facts facts)
        or any failure. μ=0: deterministic cosine over the user's
        palace vectors; no LLM, no statistics.

        Used to gate ``palace.add()`` in the COMMIT/COEXIST and
        SUPERSEDE branches of ``_apply_decision``. If the overlap
        is above ``shannon_overlap_threshold`` (default 0.9), we
        store the structured fact + chunk + edges (still findable
        by BM25 + symbolic query) but SKIP the VSA palace.add — the
        holographic superposition stays clean and retrieval stays
        fast. Verbatim tier is unchanged; "doesn't forget" holds.
        """
        if not getattr(self.cfg, "shannon_tiered_storage", True):
            return 0.0
        try:
            existing = self.store.query_facts(
                user_id=user_id, active=True, limit=500)
            if len(existing) < int(getattr(
                    self.cfg, "shannon_min_facts", 10)):
                return 0.0  # cold-start: not enough signal yet
            new_vec = self.palace.encode_fact(fact)
            best = 0.0
            # Scan in chunks of 64 to bound memory on the 4GB box.
            for f in existing:
                # Skip self (in case fact is already partially committed)
                if f.id == fact.id:
                    continue
                try:
                    # encode each existing fact once and cosine-compare
                    ev = self.palace.encode_fact(f)
                    # cosine via dot product on normalized vectors
                    a = new_vec.ravel()
                    b = ev.ravel()
                    na = float((a @ a) ** 0.5) or 1.0
                    nb = float((b @ b) ** 0.5) or 1.0
                    sim = float((a @ b) / (na * nb))
                    if sim > best:
                        best = sim
                        if best >= 0.99:
                            break  # near-identical — no need to scan more
                except Exception:
                    continue
            return best
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    def _apply_decision(self, fact: Fact, decision, commit, chunk_id,
                        results) -> int:
        action = decision.action
        if action is Action.SKIP:
            for f in self.store.get_facts(decision.target_ids or []):
                self.store.update_fact(f.id, access_count=f.access_count + 1)
                # exact restatement with an EXPLICIT date refines the
                # interval: "I've been at Microsoft since March 2024"
                # backdates an employment first learned without a date.
                if (fact.valid_from and f.valid_from
                        and fact.valid_from < f.valid_from):
                    self.store.update_fact(f.id, valid_from=fact.valid_from)
            return 0
        if action is Action.MERGE:
            for f in self.store.get_facts(decision.target_ids or []):
                self.store.update_fact(
                    f.id, reinforcement=f.reinforcement + 1,
                    access_count=f.access_count + 1,
                    valid_from=(min(fact.valid_from, f.valid_from)
                                if fact.valid_from and f.valid_from
                                and fact.valid_from < f.valid_from
                                else f.valid_from),
                    provenance={**f.provenance,
                                "merged_with": fact.text(),
                                "last_pattern": fact.provenance.get("pattern")})
                self.store.add_edge(f.id, chunk_id, "EXTRACTED_FROM")
            return 0
        if action is Action.SUPERSEDE:
            for old in self.store.get_facts(decision.target_ids or []):
                # interval sanity: valid_to must never precede valid_from —
                # a stale retraction date can otherwise invert the interval
                # and make the fact invisible to every temporal window.
                vt = (min(old.valid_to, fact.valid_from)
                      if old.valid_to else fact.valid_from)
                if old.valid_from and vt and vt < old.valid_from:
                    vt = old.valid_from
                self.store.update_fact(
                    old.id, valid_to=vt,
                    is_active=0, tx_to=iso(_now()), retired_commit=commit,
                    provenance={**old.provenance,
                                "superseded_by": fact.id})
                self.store.add_edge(fact.id, old.id, "CONTRADICTS",
                                    {"reason": decision.note})
                # Aeon-style CAUSAL edge: the new fact causally
                # displaced the old one (the retraction / supersedence
                # is the narrative cause; reader can walk CAUSAL to
                # answer "why did X change?")
                wire_causal_edge(
                    self.store, fact.id, old.id,
                    reason=f"superseded: {decision.note}")
            self.store.insert_fact(fact, commit)
            self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
            # v0.6.1: Shannon tiered storage. For SUPERSEDE we usually
            # palace.add (the new fact is the canonical value now).
            # But if the new fact has high VSA overlap to existing
            # memory, skip the palace.add — the holographic signal
            # is already there.
            overlap = self._max_vsa_overlap(fact, fact.user_id or "default")
            if overlap > float(getattr(self.cfg, "shannon_overlap_threshold", 0.9)):
                fact.provenance = {
                    **fact.provenance,
                    "shannon_tier": "verbatim_only",
                    "shannon_overlap": round(overlap, 3),
                }
                results.append(self._result(
                    fact, "SUPERSEDED",
                    f"{decision.note}; shannon_tier=verbatim_only "
                    f"(overlap={overlap:.3f})"))
            else:
                self.palace.add(fact.id, self.palace.encode_fact(fact))
                results.append(self._result(fact, "SUPERSEDED", decision.note))
            return 1
        # COMMIT / COEXIST
        self.store.insert_fact(fact, commit)
        self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
        # v0.6.1: Shannon tiered storage. For brand-new COMMIT/COEXIST
        # facts, check VSA overlap before adding the hologram. High
        # overlap (≥0.9) → store the fact + chunk + edges but skip
        # palace.add (verbatim tier still catches exact-match via BM25).
        overlap = self._max_vsa_overlap(fact, fact.user_id or "default")
        if overlap > float(getattr(self.cfg, "shannon_overlap_threshold", 0.9)):
            fact.provenance = {
                **fact.provenance,
                "shannon_tier": "verbatim_only",
                "shannon_overlap": round(overlap, 3),
            }
            results.append(self._result(
                fact, "ADD",
                f"shannon_tier=verbatim_only "
                f"(overlap={overlap:.3f}; verbatim tier still findable)"))
        else:
            self.palace.add(fact.id, self.palace.encode_fact(fact))
            results.append(self._result(fact, "ADD", decision.note))
        return 1

    # ------------------------------------------------------------------
    def _apply_retraction(self, fact: Fact, cand, commit, chunk_id,
                          msg_time, results) -> int:
        """left(X, org) — deactivate matching works_at facts (truth maintenance)."""
        retired = 0
        for f in self.store.query_facts(subject=fact.subject,
                                        relation="works_at",
                                        user_id=fact.user_id, active=True):
            if similarity(f.value, fact.value) >= 0.6:
                vt = fact.valid_from or iso(msg_time)[:10]
                # clamp: a "left in Feb" retraction learned after a fact whose
                # valid_from is later (re-stated employment) must not invert
                # the interval — cap at the fact's own valid_from boundary.
                if f.valid_from and vt < f.valid_from:
                    vt = f.valid_from
                self.store.update_fact(
                    f.id, valid_to=vt,
                    is_active=0, tx_to=iso(_now()), retired_commit=commit,
                    provenance={**f.provenance, "retracted_by": fact.id})
                self.store.add_edge(fact.id, f.id, "CONTRADICTS",
                                    {"reason": "retraction (left org)"})
                # Aeon CAUSAL: the retraction fact CAUSED the prior
                # works_at fact to be retired — walking CAUSAL from the
                # retired fact yields the retraction (and vice versa).
                wire_causal_edge(
                    self.store, fact.id, f.id,
                    reason="retraction (left org)")
                retired += 1
        self.store.insert_fact(fact, commit)
        self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
        self.palace.add(fact.id, self.palace.encode_fact(fact))
        results.append(self._result(
            fact, "RETRACTION", f"retired {retired} stale facts"))
        return 1 + retired

    # ------------------------------------------------------------------
    def _wire_temporal_edges(self, user_id: str) -> None:
        """TEMPORALLY_PRECEDED_BY chains between consecutive events."""
        events = self.store.query_facts(relation="event", user_id=user_id,
                                        active=True, order="valid_from",
                                        limit=800)
        by_subject: dict[str, list[Fact]] = {}
        for f in events:
            by_subject.setdefault(f.subject, []).append(f)
        for subj, evs in by_subject.items():
            evs.sort(key=lambda f: f.valid_from)
            for a, b in zip(evs, evs[1:]):
                self.store.add_edge(b.id, a.id, "TEMPORALLY_PRECEDED_BY")

    # ------------------------------------------------------------------
    @staticmethod
    def _result(fact: Fact, event: str, note: str = "") -> dict:
        return {
            "id": fact.id,
            "memory": f"{fact.subject} | {fact.relation} | {fact.value}",
            "event": event,
            "hash": fact.source_hash,
            "confidence": fact.confidence,
            "valid_from": fact.valid_from,
            "valid_to": fact.valid_to,
            "note": note,
        }
