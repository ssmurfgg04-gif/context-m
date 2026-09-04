"""Fact model & relation taxonomy for the Symbolic Trace.

Mirrors the Section 1.1 schema: Subject-Relation-Value triples with
bi-temporal timestamps (valid time + transaction time), confidence,
BLAKE3 source hash, scope (user/agent/flow), memory type, access count
and active flag — plus CONTRADICTS / TEMPORALLY_PRECEDED_BY /
EXTRACTED_FROM edges materialized in the store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime

from cortexm.util import parse_ts, iso

# Relations where reality has exactly one value at a time: a new value
# SUPERSEDES the old one (contradiction resolution by truth maintenance).
SINGLE_VALUED = {
    "name", "works_at", "role", "lives_in", "birthday", "age",
    "reports_to", "member_of", "studied_at", "team",
}

# Relations that accumulate: duplicates are merged, conflicts coexist.
MULTI_VALUED = {
    "likes", "dislikes", "prefers", "has_skill", "works_on", "completed", "alias",
    "sibling", "spouse", "parent", "child", "friend", "uses", "manages",
    "studied", "owns", "goal", "event", "mentioned", "joined", "left",
    "moved_to", "allergy", "has_pet", "speaks", "hobby",
}

RELATION_CATEGORIES = {
    "personal": {"name", "birthday", "age", "sibling", "spouse", "parent",
                 "child", "friend", "alias", "lives_in", "speaks", "has_pet"},
    "work": {"works_at", "role", "reports_to", "member_of", "manages",
             "team", "joined", "left", "works_on", "completed"},
    "preference": {"likes", "dislikes", "prefers"},
    "skill": {"has_skill", "studied", "studied_at"},
    "task": {"event", "goal", "mentioned", "uses", "owns"},
    "temporal": {"moved_to"},
}


@dataclass
class Fact:
    id: str
    subject: str
    relation: str
    value: str
    valid_from: str                 # when it became true in reality
    valid_to: str | None = None     # when it stopped being true (None = now)
    tx_from: str = ""               # when we recorded it
    tx_to: str | None = None
    confidence: float = 0.8
    source_hash: str = ""
    source_id: str = ""
    user_id: str = "default"
    agent_id: str | None = None
    run_id: str | None = None
    memory_type: str = "short_term"
    access_count: int = 0
    reinforcement: int = 1
    is_active: bool = True
    is_derived: bool = False
    quarantined: bool = False
    birth_commit: str | None = None
    retired_commit: str | None = None
    provenance: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def text(self) -> str:
        return f"{self.subject} | {self.relation} | {self.value}"

    def display(self) -> str:
        return f"({self.subject}, {self.relation}, {self.value})"

    def valid_window(self) -> str:
        return f"{self.valid_from}→{self.valid_to or '∞'}"

    def scope_dict(self) -> dict:
        return {"user_id": self.user_id, "agent_id": self.agent_id, "run_id": self.run_id}

    def to_row(self) -> dict:
        d = asdict(self)
        d["is_active"] = int(self.is_active)
        d["is_derived"] = int(self.is_derived)
        d["quarantined"] = int(self.quarantined)
        d["provenance"] = json.dumps(self.provenance, default=str)
        return d

    @staticmethod
    def from_row(row: dict) -> "Fact":
        row = dict(row)
        row["is_active"] = bool(row["is_active"])
        row["is_derived"] = bool(row["is_derived"])
        row["quarantined"] = bool(row.get("quarantined", 0))
        row["provenance"] = json.loads(row.get("provenance") or "{}")
        return Fact(**row)

    def matches_scope(self, user_id: str | None, agent_id: str | None = None,
                      run_id: str | None = None) -> bool:
        if user_id is not None and self.user_id != user_id:
            return False
        if agent_id is not None and self.agent_id != agent_id:
            return False
        if run_id is not None and self.run_id != run_id:
            return False
        return True


def deterministic_fact_id(*, user_id: str = "default",
                          agent_id: str | None = None,
                          run_id: str | None = None,
                          subject: str = "", relation: str = "",
                          value: str = "",
                          valid_from: str | None = None,
                          valid_to: str | None = None) -> str:
    """Content-derived fact id (32 hex, same shape as uuid4().hex).

    μ=0 stress fix: make_fact previously defaulted to ``uuid4().hex``,
    so two identical ingest runs produced different fact ids — the ids
    surface in search() results ("id 3f2a91c2"), breaking the
    byte-exact determinism the project claims. Deriving the id from
    the fact's semantic content (scope + triple + validity window,
    deliberately EXCLUDING transaction time) makes re-running the
    same corpus yield identical ids.

    Collisions (same content legitimately re-ingested after a soft
    delete, or two identical facts inserted in one batch) are handled
    by TraceStore.insert_fact, which falls back to a fresh random id
    on primary-key conflict — same behavior as the uuid4 scheme, so
    no insert path can fail.
    """
    import hashlib
    payload = "\x1f".join((
        str(user_id or ""), str(agent_id or ""), str(run_id or ""),
        str(subject or ""), str(relation or ""), str(value or ""),
        str(valid_from or ""), str(valid_to or ""),
    ))
    return hashlib.sha256(
        payload.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def make_fact(subject: str, relation: str, value: str, *,
              now: datetime, valid_from: datetime | str | None = None,
              valid_to: datetime | str | None = None, **kwargs) -> Fact:
    """Convenience constructor normalizing timestamps to ISO strings."""
    vf = iso(parse_ts(valid_from) or now)[:10] if valid_from else iso(now)[:10]
    vt = iso(parse_ts(valid_to))[:10] if valid_to else None
    fid = kwargs.pop("id", "") or ""
    if not fid:
        fid = deterministic_fact_id(
            user_id=kwargs.get("user_id", "default"),
            agent_id=kwargs.get("agent_id"),
            run_id=kwargs.get("run_id"),
            subject=subject, relation=relation, value=value,
            valid_from=vf, valid_to=vt)
    return Fact(
        id=fid,
        subject=subject, relation=relation, value=value,
        valid_from=vf, valid_to=vt, tx_from=iso(now), **kwargs)
