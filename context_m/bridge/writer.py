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

from context_m import metrics
from context_m.bridge.extractor import Extractor
from context_m.bridge.patterns import ExtractionContext
from context_m.config import Config
from context_m.security.injection import scan as injection_scan
from context_m.security.injection import contagion_scan
from context_m.trace import lifecycle
from context_m.trace.contradictions import Action
from context_m.trace.fact import Fact, make_fact
from context_m.trace.rules import RuleEngine
from context_m.trace.store import TraceStore
from context_m.util import iso, new_id, similarity, token_estimate
from context_m.vsa.palace import MemoryPalace


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

            candidates = self.extractor.extract(text, ctx)

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
                    from context_m.trace.contradictions import Action as _A
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
    def _apply_decision(self, fact: Fact, decision, commit, chunk_id,
                        results) -> int:
        action = decision.action
        if action is Action.SKIP:
            for f in self.store.get_facts(decision.target_ids or []):
                self.store.update_fact(f.id, access_count=f.access_count + 1)
            return 0
        if action is Action.MERGE:
            for f in self.store.get_facts(decision.target_ids or []):
                self.store.update_fact(
                    f.id, reinforcement=f.reinforcement + 1,
                    access_count=f.access_count + 1,
                    provenance={**f.provenance,
                                "merged_with": fact.text(),
                                "last_pattern": fact.provenance.get("pattern")})
                self.store.add_edge(f.id, chunk_id, "EXTRACTED_FROM")
            return 0
        if action is Action.SUPERSEDE:
            for old in self.store.get_facts(decision.target_ids or []):
                self.store.update_fact(
                    old.id, valid_to=min(old.valid_to, fact.valid_from)
                    if old.valid_to else fact.valid_from,
                    is_active=0, tx_to=iso(_now()), retired_commit=commit,
                    provenance={**old.provenance,
                                "superseded_by": fact.id})
                self.store.add_edge(fact.id, old.id, "CONTRADICTS",
                                    {"reason": decision.note})
            self.store.insert_fact(fact, commit)
            self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
            self.palace.add(fact.id, self.palace.encode_fact(fact))
            results.append(self._result(fact, "SUPERSEDED", decision.note))
            return 1
        # COMMIT / COEXIST
        self.store.insert_fact(fact, commit)
        self.store.add_edge(fact.id, chunk_id, "EXTRACTED_FROM")
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
                self.store.update_fact(
                    f.id, valid_to=fact.valid_from or iso(msg_time)[:10],
                    is_active=0, tx_to=iso(_now()), retired_commit=commit,
                    provenance={**f.provenance, "retracted_by": fact.id})
                self.store.add_edge(fact.id, f.id, "CONTRADICTS",
                                    {"reason": "retraction (left org)"})
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
