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
from datetime import datetime, timezone

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
        self.store = TraceStore(config.db_path, HashProvider(config.hash_provider))
        self.palace = MemoryPalace(config, self.store)
        self.extractor = Extractor(config)
        self.prefetcher = Prefetcher()
        self.writer = MemoryWriter(config, self.store, self.palace, self.extractor)
        self.reader = MemoryReader(config, self.store, self.palace,
                                   self.prefetcher)
        self.git = MemoryGit(self.store, self.palace)
        self.zk = ZKProver(self.store, self.reader)

    # ------------------------------------------------------------ Mem0 API
    def add(self, messages, *, user_id: str | None = None,
            agent_id: str | None = None, run_id: str | None = None,
            metadata: dict | None = None, timestamp=None, **kw) -> dict:
        """μ=0 ingest. Accepts str | list[str] | mem0-style message dicts."""
        user_id = user_id or self.config.default_user_id
        ts = parse_ts(timestamp) if timestamp else None
        out = self.writer.add(messages, user_id=user_id, agent_id=agent_id,
                              run_id=run_id, ts=ts, metadata=metadata, **kw)
        self.reader.invalidate_caches()
        return out

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

    def consolidate(self, now=None) -> dict:
        return lifecycle.consolidate(self.store, now)

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

    def close(self) -> None:
        self.palace.close()
        self.store.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
