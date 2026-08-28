"""Bi-temporal fact CRDT — conflict-free replicated memory state.

DATA MODEL
----------
The symbolic layer is Subject-Relation-Value triples with bi-temporal
timestamps (valid time + transaction time). Federation replicates it as a
keyed multi-version register:

* SINGLE_VALUED relations (works_at, lives_in, ...) key on
  ``(user_id, subject, relation)`` — every historical value is a VERSION in
  the register; the bi-temporal history is the version set itself.
* MULTI_VALUED relations (prefers, uses, ...) key on
  ``(user_id, subject, relation, value)`` — each distinct value is its own
  register (an OR-set member).

MERGE SEMANTICS (per key)
-------------------------
Versions are IMMUTABLE and uniquely stamped by an HLC. Merging two
replicas is the per-stamp UNION of version sets — commutative,
associative, idempotent. Resolution for reads is deterministic:

* the winning (live) version is the highest-stamped non-tombstone version
  whose stamp is greater than the highest tombstone stamp for that key
  (write-after-retract wins, retract-after-write wins — classic OR-set
  semantics under a causally-consistent total order);
* a tombstone causally after a write retracts it;
* HLC total order means every replica picks the same winner without
  coordination.

GDPR / PURGE
------------
A tombstone with ``purge=True`` is a poison pill: it retracts the key and
authorises payload GC after the retention window (see ``gc_tombstones``).
Payloads are dropped; stamps remain until every replica has seen the
tombstone (digest convergence), so late deltas cannot resurrect purged
data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cortexm.federation.hlc import HLC, parse_stamp

DEFAULT_BUCKETS = 64


def fact_key(user_id: str, subject: str, relation: str,
             value: str | None = None, single_valued: bool = True) -> str:
    """Stable CRDT key. SINGLE_VALUED keys omit the value so successive
    values become versions of one register; MULTI_VALUED keys include it."""
    parts = [user_id or "default", subject.lower(), relation.lower()]
    if not single_valued and value is not None:
        parts.append(value.lower())
    return "\x1f".join(parts)


@dataclass
class FactVersion:
    stamp: str                 # HLC string — globally unique version id
    payload: dict              # serialisable fact fields
    tombstone: bool = False
    purge: bool = False        # poison-pill tombstone (GDPR)

    def canonical(self) -> str:
        return json.dumps({"s": self.stamp, "p": self.payload,
                           "t": int(self.tombstone), "g": int(self.purge)},
                          sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]


@dataclass
class BucketStat:
    n: int = 0
    max_stamp: str = ""
    xor: int = 0


@dataclass
class FederationState:
    """The CRDT: key -> stamp -> version. Pure data; all operations are
    functions so merge logic is trivially testable."""
    node_id: str
    buckets: int = DEFAULT_BUCKETS
    _versions: dict[str, dict[str, FactVersion]] = field(default_factory=dict)

    # ---------------------------------------------------------------- write
    def put(self, key: str, payload: dict, clock: HLC,
            tombstone: bool = False, purge: bool = False,
            stamp: str | None = None) -> FactVersion:
        st = stamp or clock.tick()
        v = FactVersion(stamp=st, payload=payload, tombstone=tombstone,
                        purge=purge)
        self._versions.setdefault(key, {})[st] = v
        return v

    # ---------------------------------------------------------------- merge
    def merge_versions(self, key: str, versions: dict[str, dict]) -> int:
        """Union-merge remote versions into a key. Returns #applied."""
        slot = self._versions.setdefault(key, {})
        applied = 0
        for stamp, raw in versions.items():
            if stamp not in slot:
                slot[stamp] = FactVersion(
                    stamp=stamp,
                    payload=raw.get("payload", {}),
                    tombstone=bool(raw.get("tombstone", False)),
                    purge=bool(raw.get("purge", False)))
                applied += 1
        return applied

    def apply_delta(self, delta: dict) -> int:
        """Idempotent: re-applying the same delta changes nothing."""
        applied = 0
        for key, versions in delta.get("versions", {}).items():
            applied += self.merge_versions(key, versions)
        return applied

    # ---------------------------------------------------------------- read
    def keys(self) -> list[str]:
        return list(self._versions)

    def all_versions(self, key: str) -> list[FactVersion]:
        return sorted(self._versions.get(key, {}).values(),
                      key=lambda v: v.stamp)

    def live_versions(self, key: str) -> list[FactVersion]:
        """OR-set resolution: drop versions causally covered by a tombstone.

        A tombstone covers every version with a strictly smaller stamp. A
        purge tombstone additionally covers versions with LARGER stamps
        (poison pill — nothing may resurrect a purged key except a write
        that causally follows the purge, i.e. stamps issued after the
        purge was *received* are still allowed via the purge's own stamp
        being smaller... we keep it strict: purge kills everything with a
        different stamp than itself).
        """
        vs = self._versions.get(key, {})
        if not vs:
            return []
        tomb_stamps = [v.stamp for v in vs.values() if v.tombstone]
        purge_stamps = [v.stamp for v in vs.values() if v.purge]
        max_tomb = max(tomb_stamps) if tomb_stamps else ""
        out = []
        for v in vs.values():
            if v.tombstone:
                continue
            if v.stamp < max_tomb:
                continue                      # retracted
            if purge_stamps and v.stamp not in purge_stamps:
                continue                      # purged
            out.append(v)
        return sorted(out, key=lambda v: v.stamp)

    def winner(self, key: str) -> FactVersion | None:
        live = self.live_versions(key)
        return live[-1] if live else None

    def value_history(self, key: str) -> list[FactVersion]:
        """All live versions ordered by valid_from — the bi-temporal
        timeline for a SINGLE_VALUED key."""
        live = self.live_versions(key)

        def vf(v):
            return v.payload.get("valid_from") or ""

        return sorted(live, key=vf)

    # ------------------------------------------------------------- digests
    def bucket_of(self, key: str) -> int:
        h = int(hashlib.blake2b(key.encode(), digest_size=8).hexdigest(), 16)
        return h % self.buckets

    def digest(self) -> dict:
        """Per-bucket (count, max_stamp, xor-fold) vector. Two replicas
        with identical digests are (hash-collision aside) identical."""
        stats: dict[int, BucketStat] = {}
        for key, versions in self._versions.items():
            b = self.bucket_of(key)
            st = stats.setdefault(b, BucketStat())
            for v in versions.values():
                st.n += 1
                if v.stamp > st.max_stamp:
                    st.max_stamp = v.stamp
                st.xor ^= int(v.digest(), 16)
        return {str(b): [st.n, st.max_stamp, f"{st.xor:016x}"]
                for b, st in sorted(stats.items())}

    def delta_for(self, their_digest: dict) -> dict:
        """Ship every version of every bucket where the digests differ."""
        mine = self.digest()
        diverged = []
        for b, stat in mine.items():
            if their_digest.get(b) != stat:
                diverged.append(int(b))
        # buckets they have that I don't (still ship mine — union merge)
        for b in their_digest:
            if b not in mine:
                diverged.append(int(b))
        versions: dict[str, dict[str, dict]] = {}
        for key, vs in self._versions.items():
            if self.bucket_of(key) in diverged:
                versions[key] = {st: {"payload": v.payload,
                                      "tombstone": v.tombstone,
                                      "purge": v.purge}
                                 for st, v in vs.items()}
        return {"buckets": sorted(set(diverged)), "versions": versions}

    # ------------------------------------------------------------- gc / size
    def gc_tombstones(self, older_than_stamp: str) -> dict:
        """Drop tombstoned payloads past the retention watermark.

        Safe only once every replica has seen the tombstone (digest
        convergence); the caller enforces that. Stamps of purged keys are
        retained as empty tombstones so late deltas merge to the same
        (retracted) resolution instead of resurrecting data.
        """
        removed = {"versions": 0, "payloads": 0}
        for key, vs in list(self._versions.items()):
            for st, v in list(vs.items()):
                if v.tombstone and st < older_than_stamp:
                    if v.payload:
                        v.payload = {}
                        removed["payloads"] += 1
                    removed["versions"] += 1
        return removed

    def stats(self) -> dict:
        n_versions = sum(len(v) for v in self._versions.values())
        n_keys = len(self._versions)
        n_tomb = sum(1 for vs in self._versions.values()
                     for v in vs.values() if v.tombstone)
        return {"keys": n_keys, "versions": n_versions,
                "tombstones": n_tomb}

    # ---------------------------------------------------------- canonicalise
    def canonical_bytes(self) -> bytes:
        """Byte-exact canonical form — two converged replicas produce
        IDENTICAL bytes. This is the convergence oracle used by tests."""
        out = {}
        for key in sorted(self._versions):
            vs = self._versions[key]
            out[key] = {st: vs[st].canonical()
                        for st in sorted(vs)}
        return json.dumps(out, sort_keys=True,
                          separators=(",", ":")).encode()

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:
        return {"node_id": self.node_id, "buckets": self.buckets,
                "versions": {k: {st: {"payload": v.payload,
                                       "tombstone": v.tombstone,
                                       "purge": v.purge}
                                  for st, v in vs.items()}
                             for k, vs in self._versions.items()}}

    @classmethod
    def from_snapshot(cls, d: dict) -> "FederationState":
        st = cls(node_id=d["node_id"], buckets=d.get("buckets",
                                                     DEFAULT_BUCKETS))
        for key, vs in d.get("versions", {}).items():
            st._versions[key] = {stamp: FactVersion(
                stamp=stamp, payload=raw.get("payload", {}),
                tombstone=bool(raw.get("tombstone")),
                purge=bool(raw.get("purge"))) for stamp, raw in vs.items()}
        return st


def stamp_sort_key(stamp: str):
    wall, count, node = parse_stamp(stamp)
    return (wall, count, node)
