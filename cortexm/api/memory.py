"""Context-M Memory — the Mem0-compatible facade.

Drop-in surface (``pip install cortexm`` → ``from cortexm import Memory``):

    m = Memory()
    m.add("I work at Google", user_id="alice")
    m.search("Where does Alice work?", user_id="alice")
    m.get_all(user_id="alice")
    m.history(memory_id)

…plus everything Mem0 does not ship: Zep-compatible temporal queries,
cryptographic provenance on every retrieval, Memory Git (branch/merge/
diff/blame), ZK-lite proofs, self-healing vector storage, predictive
prefetching, federation-ready schema export, and a μ=0 ingest counter
that proves zero LLM calls.
"""

from __future__ import annotations

import datetime as _dt
import os
from datetime import datetime, timezone

import numpy as np

from cortexm import metrics
from cortexm.bridge.extractor import Extractor
from cortexm.bridge.reader import MemoryReader, RetrievalResult
from cortexm.bridge.writer import MemoryWriter
from cortexm.config import Config
from cortexm.errors import ContextMError
from cortexm.features.git import MemoryGit
from cortexm.features.prefetch import Prefetcher
from cortexm.features.zk import ZKProver
from cortexm.federation import export_schema_report, merge_schema_reports
from cortexm.security.hashes import HashProvider
from cortexm.trace import lifecycle
from cortexm.trace.store import TraceStore
from cortexm.util import parse_ts
from cortexm.vsa.palace import MemoryPalace


class Memory:
    """The Universal Neuro-Symbolic Memory Fabric — one object, every layer."""

    def __init__(self, config: Config | None = None, **overrides) -> None:
        if config is None:
            config = Config.from_env(**overrides)
        elif overrides:
            import dataclasses
            changes = {k: v for k, v in overrides.items()
                       if hasattr(config, k)}
            config = dataclasses.replace(config, **changes)
        self.config = config
        self.store = TraceStore(config.db_path, HashProvider(config.hash_provider),
                               wal_sync=getattr(config, "wal_sync", "normal"),
                               pragma_cache_mb=getattr(config, "pragma_cache_mb", 64),
                               pragma_mmap_mb=getattr(config, "pragma_mmap_mb", 256),
                               pragma_threads=getattr(config, "pragma_threads", 4),
                               pragma_temp_in_memory=getattr(config, "pragma_temp_in_memory", True),
                               pragma_locking_exclusive=getattr(config, "pragma_locking_exclusive", False))
        self.palace = MemoryPalace(config, self.store)
        self.extractor = Extractor(config)
        self.prefetcher = Prefetcher()
        self.writer = MemoryWriter(config, self.store, self.palace, self.extractor)
        self.reader = MemoryReader(config, self.store, self.palace,
                                   self.prefetcher)
        self.git = MemoryGit(self.store, self.palace)
        self.zk = ZKProver(self.store, self.reader)

        # v0.5.3: Verbatim tier — MemPalace-style FTS5 + dense over raw
        # chunks. Mounted inline (not via Context) so Memory() callers
        # get it by default. Both tiers share the SAME sqlite3 connection
        # (the store's conn) so they live in one .db file. The plugin
        # is mounted lazily so import-time failures of sqlite3 FTS5
        # (rare but possible on stripped-down builds) don't break Memory.
        self._verbatim = None
        if getattr(config, "verbatim_ingest_enabled", True) or \
                getattr(config, "verbatim_search_enabled", True):
            try:
                from cortexm.plugins.verbatim import VerbatimPlugin
                vp = VerbatimPlugin()
                # inject db + embedder manually (Memory is the kernel)
                vp._db = self.store.conn
                vp._embedder = self.palace.embedder
                vp._create_tables()
                self._verbatim = vp
                # Wire into the writer (ingest path)
                self.writer.attach_verbatim(vp)
                # Wire into the reader (search path) — reader will
                # use it via its own _verbatim_search() helper.
                self.reader.attach_verbatim(vp)
            except Exception as e:
                import sys as _sys
                print(f"[verbatim] mount failed: {e}", file=_sys.stderr)
                self._verbatim = None

        # --- enterprise layer (PII, crypto, RBAC, audit, governance) ---------
        from cortexm.security.crypto import AESGCMCipher, load_master_key
        from cortexm.security.pii import PIIGuard, PIIVault
        from cortexm.security.rbac import APIKeyStore
        from cortexm.enterprise.audit import AuditLog
        from cortexm.enterprise.governance import Governance
        self.cipher = None
        if config.encryption_at_rest:
            key = load_master_key(config.master_key_path)
            if key is None:
                sidecar = ("" if config.db_path == ":memory:"
                           else config.db_path + ".key")
                if sidecar and os.path.exists(sidecar):
                    key = load_master_key(sidecar)
            if key is None:
                from cortexm.security.crypto import generate_master_key
                if config.db_path != ":memory:":
                    key = generate_master_key(config.db_path + ".key")
            if key is not None:
                self.cipher = AESGCMCipher(key, store=self.store)
        self.pii_vault = PIIVault(self.store, self.cipher)
        self.pii_guard = PIIGuard(config.pii_mode, self.pii_vault)
        self.keys = APIKeyStore(self.store)
        self.audit_log = AuditLog(self.store, enabled=config.audit_enabled)
        self.governance = Governance(self)

    # ------------------------------------------------------------ Mem0 API
    def add(self, messages, *, user_id: str | None = None,
            agent_id: str | None = None, run_id: str | None = None,
            metadata: dict | None = None, timestamp=None, **kw) -> dict:
        """μ=0 ingest. Accepts str | list[str] | mem0-style message dicts.

        When ``pii_mode`` is ``redact``/``block``, the write path passes
        through the PII guard BEFORE extraction — raw personal data never
        reaches facts, chunks, or vectors."""
        user_id = user_id or self.config.default_user_id
        ts = parse_ts(timestamp) if timestamp else None
        if self.pii_guard.mode != "off":
            messages = self._apply_pii(messages)
            if messages is None:
                self.audit_log.log("memory.add", resource=user_id,
                               outcome="blocked_pii",
                               meta={"reason": "pii_mode=block"})
                return {"results": [], "blocked": "pii_policy"}
        out = self.writer.add(messages, user_id=user_id, agent_id=agent_id,
                              run_id=run_id, ts=ts, metadata=metadata, **kw)
        self.reader.invalidate_caches()
        if self.config.audit_actions == "all":
            self.audit_log.log("memory.add", resource=user_id,
                           meta={"facts": len(out.get("results", []))})
        # Auto-trigger FadeMem on memory pressure (Reddit ask: 16 mentions
        # of "auto-consolidate"/"memory pressure"/"context too long",
        # 2026-08-29). agentmemory does this transparently; we now do
        # too. Threshold: when a single user_id accumulates more than
        # CORTEXM_PRESSURE_THRESHOLD facts (default 2000) OR more than
        # CORTEXM_PRESSURE_CHUNKS chunks (default 500), run a lean
        # fade_sweep inline. The sweep is idempotent and bi-temporal
        # safe — deactivated facts keep their valid_from/valid_to
        # windows, so allow_inactive=True retrieval still serves them.
        self._maybe_run_fade_under_pressure(user_id)
        return out

    # ------------------------------------------------------------ mem.edit()
    def edit(self, fact_id: str, new_text: str, *,
             edited_by: str = "user", reason: str | None = None) -> dict:
        """Human-in-the-loop fact correction (Basic Memory learn,
        Reddit 2026-08-29: "human override" ≥10 mentions across
        r/LocalLLaMA + r/LangChain).

        Rewrite a fact's value AND mark its provenance with
        ``source: user_override`` so retrieval weighting can prefer
        human-corrected facts over machine-extracted ones. Audit-logged
        and hash re-verified — the original machine value is preserved
        in provenance.previous_value.

        This is the "Basic Memory" surface — every other competitor's
        memory is opaque; ours now lets a human fix what the extractor
        got wrong, with the fix carrying higher retrieval weight.
        """
        return self.update(fact_id, new_text,
                           provenance_overlay={
                               "source": "user_override",
                               "edited_by": edited_by,
                               "edit_reason": reason or "",
                               "edit_ts": datetime.now(timezone.utc).isoformat(),
                           })

    def fix(self, fact_id: str, new_text: str, *,
            edited_by: str = "user", reason: str | None = None) -> dict:
        """Alias for ``edit()`` — friendlier verb for the CLI/REPL."""
        return self.edit(fact_id, new_text, edited_by=edited_by, reason=reason)

    # ---------------------------------------------------- long-context recall
    def recall_step(self, query: str, *, user_id: str | None = None,
                   agent_id: str | None = None, run_id: str | None = None,
                   current_step: int = 0, window: int = 20,
                   k: int = 12) -> dict:
        """Asymmetric retrieval — the "memory past 20 steps" feature.

        Top-k facts RELEVANT to the query AND in danger of scrolling
        out of the LLM's context window. Multiplies the underlying
        VSA fusion score by a step-distance boost that peaks at the
        window edge (the facts the LLM is about to forget).

        See ``cortexm.api.long_recall`` for the math. μ=0 — no LLM,
        deterministic.
        """
        from cortexm.api.long_recall import recall_step as _rs
        return _rs(self, query, user_id=user_id, agent_id=agent_id,
                   run_id=run_id, current_step=current_step,
                   window=window, k=k)

    def stepped_context_block(self, query: str, *,
                              user_id: str | None = None,
                              current_step: int = 0, window: int = 20,
                              k: int = 12) -> str:
        """One-liner: return just the markdown context block ready to
        inject into the LLM system prompt. This is the drop-in
        "memory past 20 steps" UX:

            block = m.stepped_context_block(query,
                                            user_id="alice",
                                            current_step=30,
                                            window=20)
            # paste block into your agent's system prompt template
        """
        from cortexm.api.long_recall import stepped_context_block as _scb
        return _scb(self, query, user_id=user_id,
                    current_step=current_step, window=window, k=k)

    def preload_context(self, *, n: int = 20,
                        user_id: str | None = None,
                        agent_id: str | None = None,
                        run_id: str | None = None) -> str:
        """memori learn — preload the most recent N facts into the
        LLM's context on session start.

        Claude Code and other agent harnesses call this on session
        boot, paste the returned markdown block into the system prompt,
        and the LLM has immediate access to what it learned last
        session without an extra round-trip per turn.

        Different from ``stepped_context_block``: that one is
        query-biased (asymmetric retrieval toward facts about to
        scroll out of the window). This one is recency-only —
        top-N latest facts regardless of query, because at session
        start there IS no query yet.

        Reddit 2026-08-29: "preload" / "session start context" /
        "warm start" — 12 mentions across r/ClaudeCode + r/LocalLLaMA.
        """
        user_id = user_id or self.config.default_user_id
        facts = self.store.query_facts(user_id=user_id, agent_id=agent_id,
                                        run_id=run_id, active=True,
                                        limit=n * 2)
        # take the most recent N by tx_from desc
        facts = sorted(facts,
                       key=lambda f: str(getattr(f, "tx_from", "") or ""),
                       reverse=True)[:n]
        lines = [f"## Preloaded memory (top {len(facts)} recent facts, "
                 f"user={user_id})"]
        for f in facts:
            lines.append(f"- {f.subject} | {f.relation} | {f.value}  "
                         f"(conf={float(getattr(f, 'confidence', 0.0) or 0.0):.2f})")
        return "\n".join(lines)

    # ---------------------------------------------------- markdown round-trip
    def export_markdown(self, out_dir, *, user_id: str | None = None,
                        include_inactive: bool = False,
                        include_chunks: bool = True) -> dict:
        """sqlite-memory learn — dump the bi-temporal Trace as .md files
        (one per fact + one per chunk + README). Human-auditable, git-
        diff-able, portable across machines. See ``cortexm.markdown_io``.
        """
        from cortexm.markdown_io import export_markdown as _ex
        return _ex(self, out_dir=out_dir, user_id=user_id,
                   include_inactive=include_inactive,
                   include_chunks=include_chunks)

    def import_markdown(self, in_dir, *, user_id: str | None = None,
                        strategy: str = "upsert") -> dict:
        """Read markdown fact files back into the Trace. ``strategy``
        is ``upsert`` (default) or ``verify`` (dry-run). See
        ``cortexm.markdown_io.import_markdown``.
        """
        from cortexm.markdown_io import import_markdown as _im
        return _im(self, in_dir=in_dir, user_id=user_id,
                   strategy=strategy)

    # ---------------------------------------------------- session replay/fork
    def replay(self, *, user_id: str | None = None,
               from_ts: str | None = None, to_ts: str | None = None,
               n: int = 10_000) -> dict:
        """DSH-style session replay — re-emit audit-log events in
        order, optionally filtered to a time window. The audit log is
        already append-only BLAKE3-chained; this is just a read API
        over it. Reddit ≥10 mentions of "replay" / "trajectory view"
        across r/LocalLLaMA + r/agi (2026-08-29 deep dive).

        Like ``trajectory()``, if the audit log is sparse, we fall
        back to using facts as the event stream.
        """
        user_id = user_id or self.config.default_user_id
        # delegate to trajectory() to get the event list (with the
        # fallback logic), then filter by from_ts / to_ts
        traj = self.trajectory(user_id=user_id, n=n)
        events = traj["events"]
        from cortexm.util import parse_ts
        ft = parse_ts(from_ts) if from_ts else None
        tt = parse_ts(to_ts) if to_ts else None
        def _in(ev):
            ts = ev.get("ts")
            if not ts:
                return True
            try:
                ets = datetime.fromisoformat(ts.replace("Z", "+00:00")) \
                    if isinstance(ts, str) else ts
            except Exception:
                return True
            if ft and ets < ft:
                return False
            if tt and ets > tt:
                return False
            return True
        out = [e for e in events if _in(e)]
        return {"user_id": user_id, "n_events": len(out), "events": out}

    def fork(self, *, at_event_id: str | None = None,
             new_run_id: str | None = None,
             user_id: str | None = None) -> dict:
        """DSH session fork — copy the audit-log prefix up to
        ``at_event_id``, then continue from there with a new run_id.

        Implementation: returns the prefix + a fresh run_id. The
        caller (agent harness) is responsible for actually switching
        the run_id on subsequent mem.add() calls. This is the lean
        version — we don't physically copy the SQLite file, we just
        scope the new run_id to start fresh while the old one's facts
        remain queryable via ``allow_inactive=True`` retrieval.
        """
        traj = self.trajectory(user_id=user_id, n=10_000)
        events = traj["events"]
        cutoff = -1
        if at_event_id:
            for i, e in enumerate(events):
                if e.get("id") == at_event_id:
                    cutoff = i
                    break
            if cutoff < 0:
                err = ContextMError(f"fork point {at_event_id} not found "
                                    f"in session trajectory")
                err.code = "FORK_POINT_NOT_FOUND"
                raise err
        prefix = events[:cutoff + 1] if cutoff >= 0 else events
        new_run = (new_run_id or
                   f"fork-{at_event_id[:8] if at_event_id else 'all'}-"
                   f"{datetime.now(timezone.utc).strftime('%H%M%S')}")
        return {"new_run_id": new_run,
                "forked_at": at_event_id,
                "prefix_events": len(prefix),
                "prefix": prefix}

    def trajectory(self, *, user_id: str | None = None,
                   n: int = 200) -> dict:
        """Reddit "trajectory view" ask — visualizable event stream
        for the web trajectory viewer. One entry per step, in order.

        If the audit log is sparse (audit_actions='security' doesn't
        log every add), fall back to using facts as the event stream —
        sorted by tx_from. The facts themselves are the session's
        chronological event log; bi-temporal tx_from is the moment
        each fact entered the Trace.
        """
        user_id = user_id or self.config.default_user_id
        audit_events = self.audit_log.tail(n)
        out = []
        for i, e in enumerate(audit_events):
            payload = e.get("payload")
            if isinstance(payload, str):
                try:
                    import json as _json
                    payload = _json.loads(payload)
                except Exception:
                    pass
            out.append({
                "step": i,
                "id": e.get("id", ""),
                "ts": e.get("ts", ""),
                "kind": e.get("kind", ""),
                "user_id": e.get("user_id", ""),
                "payload_summary": (str(payload)[:200] if payload else ""),
                "payload": payload if isinstance(payload, dict) else {},
            })
        # Fallback: if the audit log produced fewer than 5 events, use
        # facts as the event stream. This happens when audit_actions
        # is 'security' (the default) — only security-relevant ops
        # are logged, not every memory.add. The trajectory viewer
        # should still have something to show, and facts ARE events.
        if len(out) < 5:
            facts = self.store.query_facts(user_id=user_id, active=None,
                                             limit=n)
            # sort by tx_from ascending — the order facts entered
            facts = sorted(facts,
                           key=lambda f: str(getattr(f, "tx_from", "") or ""))
            out = []
            for i, f in enumerate(facts):
                kind = ("FACT_EDITED" if (f.provenance or {}).get(
                            "source") == "user_override"
                        else "FACT_ADDED")
                if not getattr(f, "is_active", True):
                    kind = "FACT_DEACTIVATED"
                out.append({
                    "step": i,
                    "id": f.id,
                    "ts": str(getattr(f, "tx_from", "") or ""),
                    "kind": kind,
                    "user_id": f.user_id,
                    "payload_summary": f"{f.subject} | {f.relation} | "
                                        f"{f.value}",
                    "payload": {
                        "fact_id": f.id,
                        "subject": f.subject,
                        "relation": f.relation,
                        "value": f.value,
                        "confidence": float(getattr(f, "confidence", 0.0) or 0.0),
                        "valid_from": str(f.valid_from) if f.valid_from else None,
                        "valid_to": str(f.valid_to) if f.valid_to else None,
                        "source": (f.provenance or {}).get("source", "extractor"),
                    },
                })
        return {"user_id": user_id, "n_events": len(out), "events": out}

    def _maybe_run_fade_under_pressure(self, user_id: str) -> None:
        """If user_id has more than CORTEXM_PRESSURE_THRESHOLD active
        facts, run an inline fade_sweep to reclaim space. Idempotent
        and bi-temporal safe. No-op if the threshold is not crossed
        or FadeMem is disabled in config."""
        if not getattr(self.config, "fade_enabled", True):
            return
        threshold = int(os.environ.get("CORTEXM_PRESSURE_THRESHOLD",
                                       getattr(self.config,
                                               "pressure_threshold", 2000)))
        if threshold <= 0:
            return  # explicit opt-out
        try:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM facts "
                "WHERE user_id=? AND is_active=1", (user_id,)).fetchone()
            n = int(row["n"]) if row else 0
        except Exception:
            return
        if n < threshold:
            return
        try:
            from cortexm.trace.fade import fade_sweep
            fade_sweep(self.store, palace=self.palace,
                       lambda_=float(getattr(self.config, "fade_lambda", 0.05)),
                       deactivate_threshold=float(
                           getattr(self.config, "fade_deactivate_threshold", 0.30)),
                       user_id=user_id)
            self.reader.invalidate_caches()
            self.audit_log.log("memory.fade_pressure", resource=user_id,
                               meta={"facts_before": n, "threshold": threshold})
        except Exception as e:  # never let the auto-pass kill add()
            self.audit_log.log("memory.fade_pressure_failed",
                               resource=user_id,
                               meta={"error": str(e)[:200]})

    def _apply_pii(self, messages):
        """Run the PII guard over every message text. Returns redacted
        messages, or None when the policy blocks the write."""
        if isinstance(messages, str):
            res = self.pii_guard.process(messages)
            if res.blocked:
                return None
            return res.redacted_text
        if isinstance(messages, list):
            out = []
            for m in messages:
                if isinstance(m, dict) and "content" in m:
                    res = self.pii_guard.process(str(m["content"]))
                    if res.blocked:
                        return None
                    m = {**m, "content": res.redacted_text}
                elif isinstance(m, str):
                    res = self.pii_guard.process(m)
                    if res.blocked:
                        return None
                    m = res.redacted_text
                out.append(m)
            return out
        return messages

    def search(self, query: str, *, user_id: str | None = None,
               agent_id: str | None = None, run_id: str | None = None,
               limit: int | None = None, timestamp=None,
               branch: str | None = None, **kw) -> dict:
        """Neuro-symbolic retrieval with full provenance. Mem0-shaped output.

        v0.5.3: also runs recall_step (asymmetric step-distance boost)
        and concatenates its context_block onto the standard search
        result. This is the multi_session fix — facts from scrolled-out
        sessions surface via the step-distance boost, not just access_count.
        Controlled by config.recall_step_in_search (default True).
        """
        user_id = user_id or self.config.default_user_id
        ts = parse_ts(timestamp) if timestamp else None
        result = self.reader.search(query, user_id=user_id, agent_id=agent_id,
                                    run_id=run_id, k=limit, ts=ts,
                                    branch=branch)
        hits = result.facts and self.prefetcher.note_hits(
            [f.id for f in result.facts])
        context_block = result.context_block
        # v0.5.3: wire recall_step into the production search path so all
        # callers benefit. The recall_step applies an asymmetric step-
        # distance boost to facts in danger of scrolling out of the LLM's
        # context window. For LongMemEval multi_session questions ("list
        # all the places Bob has worked"), this surfaces the OLDER
        # session 1 fact that the access_count boost on the current
        # session's fact would otherwise push below top-k.
        # Gate: only fire if the user has enough ingested messages for
        # the step-distance boost to be meaningful. Below the threshold,
        # recall_step would just re-rank the same top-k as search().
        extra_timing = {}
        if getattr(self.config, "recall_step_in_search", True):
            try:
                # estimate current_step from the trace's chunk count for
                # this user — the most accurate proxy we have without an
                # explicit step counter
                try:
                    n_msgs = self.store.conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE user_id=?",
                        (user_id,)).fetchone()[0]
                except Exception:
                    n_msgs = 0
                if n_msgs >= int(getattr(
                        self.config, "recall_step_min_messages", 25)):
                    rs = self.recall_step(query, user_id=user_id,
                                         agent_id=agent_id, run_id=run_id,
                                         current_step=n_msgs,
                                         window=int(getattr(
                                             self.config, "recall_step_window", 20)),
                                         k=int(getattr(
                                             self.config, "recall_step_k", 10)))
                    rs_block = rs.get("context_block", "")
                    if rs_block:
                        context_block = (context_block + "\n\n" + rs_block
                                          if context_block else rs_block)
                        extra_timing["recall_step"] = "ran"
                        # union the rs results into result.facts so the
                        # caller sees both sets in the results list
                        rs_results = rs.get("results", [])
                        seen_ids = {f.id for f in result.facts}
                        for r in rs_results:
                            # recall_step returns dicts, not Facts; pull
                            # the underlying fact from the store if available
                            fid = r.get("id")
                            if fid and fid not in seen_ids:
                                try:
                                    fobj = self.store.get_fact(fid)
                                    if fobj and fobj.is_active and not fobj.quarantined:
                                        result.facts.append(fobj)
                                        seen_ids.add(fid)
                                except Exception:
                                    pass
                    else:
                        extra_timing["recall_step"] = "empty"
                else:
                    extra_timing["recall_step"] = f"skipped (n_msgs={n_msgs})"
            except Exception as e:
                extra_timing["recall_step_error"] = str(e)
        # merge timing
        merged_timing = dict(result.timing)
        merged_timing.update(extra_timing)
        return {
            "results": result.memories(),
            "context_block": context_block,
            "relations": [{"source": f.subject, "relationship": f.relation,
                           "destination": f.value,
                           "valid_from": f.valid_from, "valid_to": f.valid_to}
                          for f in result.facts],
            "provenance": result.provenance,
            "intent": result.intent,
            "timing": merged_timing,
            "llm_calls": 0,
        }

    def get_all(self, *, user_id: str | None = None, agent_id: str | None = None,
                run_id: str | None = None, limit: int = 200,
                branch: str | None = None) -> dict:
        user_id = user_id or self.config.default_user_id
        facts = self.store.query_facts(user_id=user_id, agent_id=agent_id,
                                       run_id=run_id, branch=branch,
                                       active=True, limit=limit)
        return {"results": [{"id": f.id,
                             "memory": f"{f.subject} | {f.relation} | {f.value}",
                             "event": "ADD",
                             "valid_from": f.valid_from,
                             "valid_to": f.valid_to,
                             "confidence": f.confidence,
                             "memory_type": f.memory_type,
                             "hash": f.source_hash} for f in facts]}

    def get(self, memory_id: str) -> dict | None:
        f = self.store.get_fact(memory_id)
        if not f:
            return None
        chunk = self.store.get_chunk(f.source_id) if f.source_id else None
        return {"id": f.id, "memory": f"{f.subject} | {f.relation} | {f.value}",
                "event": "ADD", "valid_from": f.valid_from,
                "valid_to": f.valid_to, "confidence": f.confidence,
                "hash": f.source_hash, "source": chunk["text"] if chunk else None,
                "verified": bool(chunk and chunk["hash"] == f.source_hash)}

    def history(self, memory_id: str) -> list[dict]:
        """Full bi-temporal history of a fact chain (supersessions included)."""
        f = self.store.get_fact(memory_id)
        if not f:
            return []
        chain = [f]
        seen = {f.id}
        frontier = [f.id]
        while frontier:
            nxt = []
            for fid in frontier:
                for e in self.store.edges_of(fid, "CONTRADICTS", "out"):
                    other = self.store.get_fact(e["dst"])
                    if other and other.id not in seen:
                        seen.add(other.id)
                        chain.append(other)
                        nxt.append(other.id)
                for e in self.store.edges_of(fid, "CONTRADICTS", "in"):
                    other = self.store.get_fact(e["src"])
                    if other and other.id not in seen:
                        seen.add(other.id)
                        chain.append(other)
                        nxt.append(other.id)
            frontier = nxt
        chain.sort(key=lambda x: (x.valid_from, x.tx_from))
        return [{"id": c.id,
                 "memory": f"{c.subject} | {c.relation} | {c.value}",
                 "event": "ADD" if c.is_active else ("QUARANTINED" if c.quarantined else "SUPERSEDED"),
                 "valid_from": c.valid_from, "valid_to": c.valid_to,
                 "recorded_at": c.tx_from} for c in chain]

    def update(self, memory_id: str, data: str,
               *, provenance_overlay: dict | None = None) -> dict:
        """Rewrite a fact's value (audit-logged, hash re-verified).

        ``provenance_overlay`` (mem.edit / mem.fix path): merge these
        keys into the fact's provenance dict on top of the standard
        ``manual_update`` / ``previous_value`` markers. Used by
        ``mem.edit()`` to stamp ``source: user_override`` so the
        reader can weight human-corrected facts higher than
        machine-extracted ones (Basic Memory learn).
        """
        f = self.store.get_fact(memory_id)
        if not f:
            raise ContextMError(f"no fact {memory_id}")
        old = f.value
        new_prov = {**f.provenance,
                    "manual_update": True,
                    "previous_value": old}
        if provenance_overlay:
            new_prov = {**new_prov, **provenance_overlay}
        self.store.update_fact(memory_id, value=data,
                               provenance=new_prov)
        self.palace.add(memory_id, self.palace.vsa.encode_fact(
            self.palace.embedder.embed(f.subject),
            self.palace.embedder.embed(f.relation),
            self.palace.embedder.embed(data)))
        return {"id": memory_id, "event": "UPDATE",
                "previous_value": old, "new_value": data}

    def delete(self, memory_id: str) -> dict:
        f = self.store.get_fact(memory_id)
        if not f:
            return {"id": memory_id, "event": "NOOP"}
        self.store.update_fact(memory_id, is_active=0,
                               provenance={**f.provenance, "deleted": True})
        return {"id": memory_id, "event": "DELETE"}

    def delete_all(self, *, user_id: str | None = None) -> dict:
        user_id = user_id or self.config.default_user_id
        facts = self.store.query_facts(user_id=user_id, active=True)
        for f in facts:
            self.store.update_fact(f.id, is_active=0,
                                   provenance={**f.provenance, "deleted": True})
        return {"event": "DELETE_ALL", "count": len(facts)}

    def users(self) -> list[str]:
        rows = self.store.conn.execute(
            "SELECT DISTINCT user_id FROM facts").fetchall()
        return [r["user_id"] for r in rows]

    def reset(self) -> None:
        self.store.conn.executescript(
            "DELETE FROM facts; DELETE FROM chunks; DELETE FROM edges; "
            "DELETE FROM vectors; DELETE FROM commits; DELETE FROM branches; "
            "DELETE FROM kv;")
        self.store.conn.commit()
        self.store._ensure_genesis()
        self.palace = MemoryPalace(self.config, self.store)
        self.writer = MemoryWriter(self.config, self.store, self.palace,
                                   self.extractor)
        self.reader = MemoryReader(self.config, self.store, self.palace,
                                   self.prefetcher)
        self.git = MemoryGit(self.store, self.palace)
        self.zk = ZKProver(self.store, self.reader)

    # -------------------------------------------------- Zep temporal API
    def get_between(self, start, end, *, user_id: str | None = None,
                    field: str = "valid") -> list[dict]:
        user_id = user_id or self.config.default_user_id
        s = parse_ts(start)
        e = parse_ts(end)
        facts = self.store.temporal_window(
            s.isoformat() if s else None, e.isoformat() if e else None,
            user_id=user_id, field=field, active=False)
        return [{"id": f.id, "fact": f.display(),
                 "valid_from": f.valid_from, "valid_to": f.valid_to,
                 "is_active": f.is_active} for f in facts]

    def get_before(self, ts, *, user_id: str | None = None,
                   field: str = "valid") -> list[dict]:
        return self.get_between("1970-01-01", ts, user_id=user_id, field=field)

    def get_after(self, ts, *, user_id: str | None = None,
                  field: str = "valid") -> list[dict]:
        return self.get_between(ts, "9999-12-31", user_id=user_id, field=field)

    # -------------------------------------------------- provenance & trust
    def audit(self, query: str, *, user_id: str | None = None) -> dict:
        """The 'Why' audit trail: query → VSA match → symbolic dereference →
        source hash → original text, for every returned fact."""
        res = self.search(query, user_id=user_id)
        return {"query": query,
                "verification": res["provenance"]["verification"],
                "chain": res["provenance"]["chain"],
                "llm_calls": 0}

    def verify_integrity(self, sample: int | None = None) -> dict:
        """Recompute source hashes + vector hashes (tamper detection)."""
        chunks = self.store.conn.execute(
            "SELECT id, text, hash FROM chunks").fetchall()
        bad_chunks = 0
        for c in chunks:
            if self.store.hasher.hash_text(c["text"]) != c["hash"]:
                bad_chunks += 1
        vec = self.palace.health_check(sample)
        return {"chunks_checked": len(chunks), "corrupt_chunks": bad_chunks,
                "vector_check": {k: v for k, v in vec.items()
                                 if k != "corrupt_ids"},
                "hash_provider": self.store.hasher.name,
                "ok": bad_chunks == 0 and vec["corrupt"] == 0}

    # -------------------------------------------------- Memory Git
    def branch(self, name: str, from_commit: str | None = None,
               switch: bool = True) -> str:
        out = self.git.branch(name, from_commit, switch)
        self.reader.invalidate_caches()
        return out

    def checkout(self, name: str) -> None:
        self.git.checkout(name)
        self.reader.invalidate_caches()

    def merge(self, name: str, strategy: str = "latest-wins") -> dict:
        out = self.git.merge(name, strategy)
        self.reader.invalidate_caches()
        return out

    def diff(self, a: str, b: str) -> dict:
        return self.git.diff(a, b)

    def blame(self, subject: str, relation: str | None = None) -> list[dict]:
        return self.git.blame(subject, relation)

    def log(self, limit: int = 50) -> list[dict]:
        return self.git.log(limit=limit)

    # -------------------------------------------------- ZK-lite
    def prove(self, query: str, *, user_id: str | None = None,
              threshold: float = 0.2) -> dict:
        return self.zk.prove(query, user_id=user_id or
                             self.config.default_user_id,
                             threshold=threshold)

    def verify_proof(self, proof: dict) -> bool:
        return self.zk.verify(proof)

    # -------------------------------------------------- self-healing
    def health_check(self, sample: int | None = None) -> dict:
        return self.palace.health_check(sample)

    def heal(self) -> dict:
        facts = {f.id: f for f in self.store.query_facts(active=None)}
        return self.palace.heal(facts)

    def corrupt(self, rate: float, seed: int = 0, persist: bool = False) -> int:
        return self.palace.corrupt(rate, seed=seed, persist=persist)

    # -------------------------------------------------- lifecycle & ops
    def apply_rules(self) -> int:
        """Deferred Datalog materialization after bulk ingest."""
        self.store.begin_batch()
        derived = self.writer.apply_rules()
        self.reader.invalidate_caches()
        return len(derived)

    # -------------------------------------------------- async enrichment
    def enrich(self, user_id: str | None = None, *, extractor=None,
               limit: int | None = None, min_confidence: float | None = None,
               dry_run: bool = False) -> dict:
        """Post-store LLM enrichment fallback (μ=0 stays intact on the
        synchronous ingest path; this is the plan's graceful-degradation
        second pass over zero-signal chunks). Returns an EnrichmentReport
        as a dict — LLM call count and provenance markers are auditable.
        """
        from cortexm.bridge.enrich import enrich as _enrich
        rep = _enrich(self.writer, user_id, extractor=extractor,
                      limit=limit, min_confidence=min_confidence,
                      dry_run=dry_run)
        self.reader.invalidate_caches()
        return rep.to_dict()

    def enrich_async(self, user_id: str | None = None, **kw):
        """Background-thread variant of enrich(). Returns (thread, holder)."""
        from cortexm.bridge.enrich import enrich_async as _ea
        return _ea(self.writer, user_id, **kw)

    # -------------------------------------------------- scope sandbox
    def promote(self, fact_ids: list[str], *, reviewed_by: str = "system",
                force: bool = False) -> dict:
        """Promote agent-scoped facts into the user scope (InjecMEM policy).

        Gated on confidence + a fresh InjecMEM/MINJA rescan of the source
        chunk; every decision lands in the tamper-evident audit chain.
        """
        from cortexm.security.sandbox import ScopeSandbox
        sandbox = ScopeSandbox(self.config, self.store, self.audit_log)
        out = sandbox.promote(fact_ids, reviewed_by=reviewed_by, force=force)
        self.reader.invalidate_caches()
        return out

    def consolidate(self, now=None, **kwargs) -> dict:
        """Run BOTH consolidation passes:

        (1) lifecycle.consolidate — Dual-Layer Agentic Memory: promote
            reinforced short-term facts, decay untouched ones, demote
            weak long-term facts. Fast, pure-SQL.

        (2) trace.consolidate.consolidate — Aeon-inspired "dreaming":
            merge redundant triples (MERGED_WITH edges), retire stale
            facts past valid_to + grace, defrag palace, retrain
            MBTB prefetcher. Slower, idempotent, safe.

        (3) trace.cognition — HMS-style self-organization:
            PatternScanner + AbstractionEngine + GapDetector +
            HypothesisEngine + AnalogyDetector. Emits HYPOTHESIZED_BY
            edges with confidence < 0.5 — never promoted to active
            retrieval unless explicitly confirmed by user input.

        Returns a combined report. Either pass may be skipped via
        kwargs lifecycle=False / dreaming=False / cognition=False.
        """
        out = {"lifecycle": {}, "dreaming": {}}
        if kwargs.get("lifecycle", True):
            out["lifecycle"] = lifecycle.consolidate(self.store, now)
        if kwargs.get("dreaming", True):
            from cortexm.trace.consolidate import consolidate as _dream
            # Respect the Config's fade_enabled / tmt_enabled /
            # cognition_enabled flags by default so `cortexm
            # consolidate` runs the full production pass without
            # requiring CLI flag plumbing. CLI / env can still turn
            # them off via kwargs.
            run_fade = kwargs.get("run_fade",
                                   getattr(self.config, "fade_enabled", True))
            run_tmt = kwargs.get("run_tmt",
                                  getattr(self.config, "tmt_enabled", False))
            run_cognition = kwargs.get(
                "run_cognition",
                getattr(self.config, "cognition_enabled", False))
            out["dreaming"] = _dream(self.store, palace=self.palace,
                                       prefetcher=self.prefetcher,
                                       user_id=kwargs.get("user_id"),
                                       dry_run=kwargs.get("dry_run", False),
                                       run_fade=run_fade,
                                       run_tmt=run_tmt,
                                       run_cognition=run_cognition)
        return out

    def export_schema_report(self, user_id: str | None = None) -> dict:
        return export_schema_report(self.store, user_id)

    @staticmethod
    def merge_schema_reports(reports: list[dict]) -> dict:
        return merge_schema_reports(reports)

    # -------------------------------------------------- introspection
    def stats(self) -> dict:
        s = self.store.stats()
        s.update(self.palace.storage_stats())
        s.update(self.reader.slb.stats())
        s.update(self.prefetcher.stats())
        s["counters"] = metrics.counters()
        s["u0_protocol"] = "verified" if metrics.llm_calls() == 0 else "VIOLATED"
        s["hash_provider"] = self.store.hasher.name
        s["vsa_mode"] = self.config.vsa_mode
        s["dims"] = self.config.dims
        return s

    def storage_stats(self) -> dict:
        return self.palace.storage_stats()

    # -------------------------------------------------- sidecar blob arena
    def enable_blob_arena(self, path: str | os.PathLike) -> dict:
        """Opt-in: migrate chunks.text into a sidecar mmap-backed blob
        file (Aeon-inspired). After migration:

          - chunks.text      := first 64 bytes of the source (preview)
          - chunks.blob_offset := byte offset in the arena file
          - chunks.blob_len  := payload length
          - chunks.blob_compressed := 0/1

        Graph queries that only need the preview still work without
        touching the arena. Full text is fetched on demand via
        arena.get_text(offset, len, compressed).

        Returns a migration report dict.
        """
        from cortexm.trace.blob_arena import (
            BlobArena, migrate_chunks_to_arena)
        arena = BlobArena(path)
        report = migrate_chunks_to_arena(self.store, arena)
        # keep arena handle on self so it stays alive for the life of
        # the Memory instance; the host can grab it via .blob_arena
        self.blob_arena = arena
        return report

    def get_chunk_text(self, chunk_id: str) -> str:
        """Fetch full text for a chunk — from the arena if migrated,
        otherwise from the inline text column."""
        arena = getattr(self, "blob_arena", None)
        if arena is not None:
            from cortexm.trace.blob_arena import get_chunk_text as _g
            return _g(self.store, arena, chunk_id)
        row = self.store.conn.execute(
            "SELECT text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return row[0] if row else ""

    # ------------------------------------- engineered role vectors (NSR)
    def use_engineered_role_vectors(self, *, n_epochs: int = 200,
                                     lr: float = 0.01,
                                     save_path: str | None = None,
                                     verbose: bool = False) -> dict:
        """NSR-inspired: train a tiny autoencoder on the actual fact
        corpus and use the top-k principal directions as role vectors.

        arXiv insight: random role vectors (current default) waste
        capacity on directions orthogonal to the data. Engineered ones
        sit on the data's principal axes — higher effective capacity,
        lower cross-talk, better retrieval SNR.

        After this call, the palace's VSA will use the engineered role
        vectors for bind() / unbind() / probe() operations. New facts
        ingested after this call will be encoded with the engineered
        vectors; previously-encoded facts keep their original (random)
        holograms until the palace is rebuilt.

        Returns the autoencoder training report.

        NOTE: this is OPT-IN. The default behavior (random role
        vectors) is unchanged unless this method is called.
        """
        from cortexm.vsa.role_vectors import EngineeredRoleVectors
        # pull the fact matrix from the palace — we need the actual
        # S/R/V vectors used to encode the existing facts
        facts = self.store.query_facts(active=True)
        if not facts:
            return {"trained": False, "reason": "no_facts_in_store"}
        # build a (n_facts * 3, dims) matrix: S, R, V vectors for each
        # fact, stacked. Each row is a single role-filler vector.
        rows = []
        for f in facts:
            for field, role in (
                    (f.subject, "S"), (f.relation, "R"), (f.value, "V")):
                if field:
                    rows.append(self.palace.embedder.embed(field))
        if not rows:
            return {"trained": False, "reason": "no_text_to_embed"}
        matrix = np.stack(rows).astype(np.float32)
        # cap the matrix size — 10k samples is plenty for PCA
        if len(matrix) > 10_000:
            import numpy as _np
            rng = _np.random.default_rng(self.config.seed)
            idx = rng.choice(len(matrix), 10_000, replace=False)
            matrix = matrix[idx]
        erv = EngineeredRoleVectors(
            dims=self.config.dims, n_roles=3,
            seed=self.config.seed, n_epochs=n_epochs, lr=lr)
        report = erv.fit(matrix)
        if erv.is_fit:
            self.palace.vsa.use_engineered(erv)
            self._engineered_role_vectors = erv
            if save_path:
                erv.save(save_path)
            if verbose:
                print(f"[engineered-role-vectors] {report}")
        return report

    def save_engineered_role_vectors(self, path: str) -> None:
        erv = getattr(self, "_engineered_role_vectors", None)
        if erv is None:
            raise RuntimeError("no engineered role vectors to save — "
                                "call use_engineered_role_vectors() first")
        erv.save(path)

    def load_engineered_role_vectors(self, path: str) -> dict:
        """Load a previously-saved .npz of engineered role vectors
        and swap them in as the active role vectors."""
        from cortexm.vsa.role_vectors import EngineeredRoleVectors
        erv = EngineeredRoleVectors(dims=self.config.dims,
                                     seed=self.config.seed)
        erv.load(path)
        if erv.is_fit:
            self.palace.vsa.use_engineered(erv)
            self._engineered_role_vectors = erv
            return {"loaded": True, "path": path}
        return {"loaded": False, "reason": "file_empty_or_corrupt"}

    def close(self) -> None:
        self.palace.close()
        self.store.close()

    # ------------------------------------------------------------------
    # v0.6.1: BM25 + index maintenance facade
    # Exposed on Memory so users can call ``m.tune_bm25(k1=1.2, b=0.5)``
    # and ``m.optimize_index()`` without reaching into the verbatim
    # plugin. If the verbatim tier isn't mounted (e.g. disabled in
    # Config), these are no-ops so callers don't need to defensively
    # check.
    def tune_bm25(self, k1: float = 1.2, b: float = 0.75) -> None:
        """Tune BM25 k1 (term saturation) + b (length normalization).

        Lucene defaults: k1=1.2, b=0.75. Our corpus (short chat chunks,
        avg ~12 tokens) tends to prefer slightly higher saturation and
        weaker length norm; the v0.6.0 defaults were k1=1.5, b=0.75.
        Run ``scripts/tune_bm25_canonical.py`` to grid-search on the
        canonical LongMemEval sample and pick the best for your data.

        Takes effect on the next ``search()`` call.
        """
        if self._verbatim is not None:
            self._verbatim.tune_bm25(k1=k1, b=b)
        # Persist on the config too so reopens honor the tuning.
        self.config.bm25_k1 = k1
        self.config.bm25_b = b

    def optimize_index(self) -> dict:
        """VACUUM + FTS5 optimize + WAL checkpoint.

        Run this after large bulk ingests to fold the WAL back into
        the main .db file, reclaim deleted-page space, and merge the
        FTS5 b-tree segments. Idempotent; safe to call any time.
        """
        if self._verbatim is not None:
            return self._verbatim.optimize_index()
        return {"optimized": False, "reason": "verbatim tier not mounted"}

    def _reopen(self) -> None:
        """Rebind every component to a freshly-opened store (post-restore)."""
        from cortexm.security.pii import PIIGuard, PIIVault
        from cortexm.security.rbac import APIKeyStore
        from cortexm.enterprise.audit import AuditLog
        from cortexm.enterprise.governance import Governance
        self.store = TraceStore(self.config.db_path,
                                HashProvider(self.config.hash_provider),
                                wal_sync=getattr(self.config, "wal_sync", "normal"),
                                pragma_cache_mb=getattr(self.config, "pragma_cache_mb", 64),
                                pragma_mmap_mb=getattr(self.config, "pragma_mmap_mb", 256),
                                pragma_threads=getattr(self.config, "pragma_threads", 4),
                                pragma_temp_in_memory=getattr(self.config, "pragma_temp_in_memory", True),
                                pragma_locking_exclusive=getattr(self.config, "pragma_locking_exclusive", False))
        self.palace = MemoryPalace(self.config, self.store)
        self.writer = MemoryWriter(self.config, self.store, self.palace,
                                   self.extractor)
        self.reader = MemoryReader(self.config, self.store, self.palace,
                                   self.prefetcher)
        self.git = MemoryGit(self.store, self.palace)
        self.zk = ZKProver(self.store, self.reader)
        self.pii_vault = PIIVault(self.store, self.cipher)
        self.pii_guard = PIIGuard(self.config.pii_mode, self.pii_vault)
        self.keys = APIKeyStore(self.store)
        self.audit_log = AuditLog(self.store, enabled=self.config.audit_enabled)
        self.governance = Governance(self)
        # the governance object captured the OLD store/palace in __init__;
        # rebind to the fresh ones
        self.governance.store = self.store
        self.governance.palace = self.palace
        self.governance.audit = self.audit_log

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
