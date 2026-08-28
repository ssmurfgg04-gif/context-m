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

from context_m import metrics
from context_m.bridge.extractor import Extractor
from context_m.bridge.reader import MemoryReader, RetrievalResult
from context_m.bridge.writer import MemoryWriter
from context_m.config import Config
from context_m.errors import ContextMError
from context_m.features.git import MemoryGit
from context_m.features.prefetch import Prefetcher
from context_m.features.zk import ZKProver
from context_m.federation import export_schema_report, merge_schema_reports
from context_m.security.hashes import HashProvider
from context_m.trace import lifecycle
from context_m.trace.store import TraceStore
from context_m.util import parse_ts
from context_m.vsa.palace import MemoryPalace


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
                               wal_sync=getattr(config, "wal_sync", "normal"))
        self.palace = MemoryPalace(config, self.store)
        self.extractor = Extractor(config)
        self.prefetcher = Prefetcher()
        self.writer = MemoryWriter(config, self.store, self.palace, self.extractor)
        self.reader = MemoryReader(config, self.store, self.palace,
                                   self.prefetcher)
        self.git = MemoryGit(self.store, self.palace)
        self.zk = ZKProver(self.store, self.reader)

        # --- enterprise layer (PII, crypto, RBAC, audit, governance) ---------
        from context_m.security.crypto import AESGCMCipher, load_master_key
        from context_m.security.pii import PIIGuard, PIIVault
        from context_m.security.rbac import APIKeyStore
        from context_m.enterprise.audit import AuditLog
        from context_m.enterprise.governance import Governance
        self.cipher = None
        if config.encryption_at_rest:
            key = load_master_key(config.master_key_path)
            if key is None:
                sidecar = ("" if config.db_path == ":memory:"
                           else config.db_path + ".key")
                if sidecar and os.path.exists(sidecar):
                    key = load_master_key(sidecar)
            if key is None:
                from context_m.security.crypto import generate_master_key
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
        return out

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
        """Neuro-symbolic retrieval with full provenance. Mem0-shaped output."""
        user_id = user_id or self.config.default_user_id
        ts = parse_ts(timestamp) if timestamp else None
        result = self.reader.search(query, user_id=user_id, agent_id=agent_id,
                                    run_id=run_id, k=limit, ts=ts,
                                    branch=branch)
        hits = result.facts and self.prefetcher.note_hits(
            [f.id for f in result.facts])
        return {
            "results": result.memories(),
            "context_block": result.context_block,
            "relations": [{"source": f.subject, "relationship": f.relation,
                           "destination": f.value,
                           "valid_from": f.valid_from, "valid_to": f.valid_to}
                          for f in result.facts],
            "provenance": result.provenance,
            "intent": result.intent,
            "timing": result.timing,
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

    def update(self, memory_id: str, data: str) -> dict:
        """Rewrite a fact's value (audit-logged, hash re-verified)."""
        f = self.store.get_fact(memory_id)
        if not f:
            raise ContextMError(f"no fact {memory_id}")
        old = f.value
        self.store.update_fact(memory_id, value=data,
                               provenance={**f.provenance,
                                           "manual_update": True,
                                           "previous_value": old})
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
        from context_m.bridge.enrich import enrich as _enrich
        rep = _enrich(self.writer, user_id, extractor=extractor,
                      limit=limit, min_confidence=min_confidence,
                      dry_run=dry_run)
        self.reader.invalidate_caches()
        return rep.to_dict()

    def enrich_async(self, user_id: str | None = None, **kw):
        """Background-thread variant of enrich(). Returns (thread, holder)."""
        from context_m.bridge.enrich import enrich_async as _ea
        return _ea(self.writer, user_id, **kw)

    # -------------------------------------------------- scope sandbox
    def promote(self, fact_ids: list[str], *, reviewed_by: str = "system",
                force: bool = False) -> dict:
        """Promote agent-scoped facts into the user scope (InjecMEM policy).

        Gated on confidence + a fresh InjecMEM/MINJA rescan of the source
        chunk; every decision lands in the tamper-evident audit chain.
        """
        from context_m.security.sandbox import ScopeSandbox
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
            from context_m.trace.consolidate import consolidate as _dream
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
        from context_m.trace.blob_arena import (
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
            from context_m.trace.blob_arena import get_chunk_text as _g
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
        from context_m.vsa.role_vectors import EngineeredRoleVectors
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
        from context_m.vsa.role_vectors import EngineeredRoleVectors
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

    def _reopen(self) -> None:
        """Rebind every component to a freshly-opened store (post-restore)."""
        from context_m.security.pii import PIIGuard, PIIVault
        from context_m.security.rbac import APIKeyStore
        from context_m.enterprise.audit import AuditLog
        from context_m.enterprise.governance import Governance
        self.store = TraceStore(self.config.db_path,
                                HashProvider(self.config.hash_provider),
                                wal_sync=getattr(self.config, "wal_sync", "normal"))
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
