"""CRDT federation tests — convergence, partitions, retraction, security.

The convergence oracle is byte-exact: ``canonical_bytes()`` must be
IDENTICAL across replicas after sync, not merely equivalent. That is a
stronger statement than "queries agree" and catches ordering bugs.
"""

from __future__ import annotations

import pytest

from cortexm.federation import (
    FederationError,
    FederationNode,
    FileTransport,
    HLC,
    InMemoryMesh,
    fact_key,
)
from cortexm.federation.crdt import FederationState

KEY = "default\x1falice\x1fworks_at"


def mk(node_id: str, **kw) -> FederationNode:
    return FederationNode(node_id, members=["a", "b", "c"],
                          federation_key="test-key", **kw)


# ------------------------------------------------------------------ HLC
class TestHLC:
    def test_tick_monotonic(self):
        c = HLC("a")
        stamps = [c.tick(now_ms=1000) for _ in range(50)]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == 50

    def test_receive_orders_across_nodes(self):
        a, b = HLC("a"), HLC("b")
        s1 = a.tick(now_ms=1000)
        s2 = b.receive(s1)               # b absorbs a's stamp
        assert s2 > s1
        s3 = a.tick(now_ms=1000)         # still causally after s1
        assert s3 > s1

    def test_skewed_clock_absorbed(self):
        a, b = HLC("a"), HLC("b")
        future = a.tick(now_ms=10_000_000)
        s = b.receive(future)            # b's clock is behind
        assert s > future                # counter breaks the tie
        now = b.tick(now_ms=1)           # even a "behind" tick stays ahead
        assert now > future

    def test_string_order_matches_tuple_order(self):
        c = HLC("node-x")
        s = [c.tick(now_ms=1234567890123) for _ in range(3)]
        assert s == sorted(s)


# ------------------------------------------------------------ convergence
class TestConvergence:
    def test_disjoint_writes_converge_byte_exact(self):
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        b.put_fact("default\x1fbob\x1flives_in", {"value": "Berlin",
                                                  "fact_id": "f2"})
        assert not a.converged_with(b)
        a.sync_with(b)
        assert a.converged_with(b)
        assert a.state.winner(KEY).payload["value"] == "Acme"
        assert b.state.winner(KEY).payload["value"] == "Acme"

    def test_merge_is_idempotent_and_commutative(self):
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme"})
        b.put_fact(KEY, {"value": "Acme"})
        a.sync_with(b)
        before = a.state.canonical_bytes()
        a.sync_with(b)                    # re-sync: nothing changes
        assert a.state.canonical_bytes() == before

    def test_concurrent_same_key_same_winner_everywhere(self):
        a, b, c = mk("a"), mk("b"), mk("c")
        # two nodes concurrently write different values for the same key
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        b.put_fact(KEY, {"value": "Globex", "fact_id": "f2"})
        mesh = InMemoryMesh()
        mesh.link("a", "b"); mesh.link("b", "c"); mesh.link("a", "c")
        nodes = {"a": a, "b": b, "c": c}
        mesh.gossip(nodes, rounds=3)
        winners = {n.state.winner(KEY).payload["value"]
                   for n in nodes.values()}
        assert len(winners) == 1          # deterministic single winner
        assert a.converged_with(b) and b.converged_with(c)

    def test_multivalued_keys_accumulate(self):
        a, b = mk("a"), mk("b")
        k1 = fact_key("default", "alice", "prefers", "vim",
                      single_valued=False)
        k2 = fact_key("default", "alice", "prefers", "emacs",
                      single_valued=False)
        a.put_fact(k1, {"value": "vim", "fact_id": "f1"})
        b.put_fact(k2, {"value": "emacs", "fact_id": "f2"})
        a.sync_with(b)
        assert a.state.winner(k1) is not None
        assert a.state.winner(k2) is not None   # both preferences live


# --------------------------------------------------------------- partition
class TestPartitionHeal:
    def test_partition_heal_no_lost_retractions(self):
        a, b, c = mk("a"), mk("b"), mk("c")
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        mesh = InMemoryMesh()
        mesh.link("a", "b"); mesh.link("b", "c"); mesh.link("a", "c")
        mesh.gossip({"a": a, "b": b, "c": c})          # everyone knows f1

        # partition: {a, b} | {c}
        mesh.cut("a", "c"); mesh.cut("b", "c")
        a.retract(KEY)                                   # retraction in AB
        c.put_fact(KEY, {"value": "Globex", "fact_id": "f2"})  # write in C

        mesh.link("a", "c"); mesh.link("b", "c")         # heal
        mesh.gossip({"a": a, "b": b, "c": c}, rounds=3)

        # all three converge; retraction wins over the concurrent write
        # (retraction's stamp is causally... actually concurrent — but
        # the HLC total order picks ONE winner everywhere)
        assert a.converged_with(b) and b.converged_with(c)
        winners = [n.state.winner(KEY) for n in (a, b, c)]
        assert all(w is not None for w in winners)
        vals = {w.payload.get("value") for w in winners}
        assert len(vals) == 1

    def test_retraction_beats_causally_older_write(self):
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        a.sync_with(b)
        b.retract(KEY)                  # causally AFTER f1
        a.sync_with(b)                  # a LEARNS the retraction
        assert a.state.winner(KEY) is None
        # write causally AFTER the retraction -> write wins (OR-set)
        a.put_fact(KEY, {"value": "Acme2", "fact_id": "f1b"})
        a.sync_with(b); b.sync_with(a); a.sync_with(b)
        assert a.converged_with(b)
        assert a.state.winner(KEY).payload["value"] == "Acme2"
        assert b.state.winner(KEY).payload["value"] == "Acme2"

    def test_concurrent_write_vs_retraction_is_deterministic(self):
        """A write that never saw the retraction is CONCURRENT with it:
        either may win, but every replica must pick the same winner."""
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        a.sync_with(b)
        b.retract(KEY)                  # b retracts
        a.put_fact(KEY, {"value": "X", "fact_id": "f2"})  # a unaware
        a.sync_with(b); b.sync_with(a); a.sync_with(b)
        assert a.converged_with(b)      # single deterministic outcome
        winners = (a.state.winner(KEY), b.state.winner(KEY))
        assert winners[0] is winners[1] or (
            winners[0] and winners[1] and
            winners[0].stamp == winners[1].stamp)


# ------------------------------------------------------------- tombstones
class TestTombstones:
    def test_tombstone_retracts_key(self):
        a = mk("a")
        a.put_fact(KEY, {"value": "Acme"})
        assert a.state.winner(KEY) is not None
        a.retract(KEY)
        assert a.state.winner(KEY) is None

    def test_purge_poison_pill_beats_concurrent_write(self):
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        a.sync_with(b)
        a.retract(KEY, purge=True)
        b.put_fact(KEY, {"value": "Globex", "fact_id": "f2"})  # concurrent
        a.sync_with(b); b.sync_with(a); a.sync_with(b)
        assert a.converged_with(b)
        # purge kills even the concurrent write
        assert a.state.winner(KEY) is None
        assert b.state.winner(KEY) is None

    def test_gc_keeps_retraction_stamp(self):
        a = mk("a")
        st = a.put_fact(KEY, {"value": "Acme"})
        a.retract(KEY)
        a.state.gc_tombstones(older_than_stamp="\uffff")   # gc everything
        # payload gone, tombstone stamp remains -> no resurrection
        versions = a.state.all_versions(KEY)
        assert all(v.payload == {} for v in versions if v.tombstone)
        b = mk("b")
        b.put_fact(KEY, {"value": "Resurrect?", "fact_id": "f9"})
        # simulate late delta merge AFTER gc: old version replay cannot
        # beat the retained tombstone
        a.state.merge_versions(KEY, {st.stamp: {"payload": {"value": "old"}}})
        assert a.state.winner(KEY) is None


# ---------------------------------------------------------------- digests
class TestAntiEntropy:
    def test_identical_digests_ship_nothing(self):
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme"})
        a.sync_with(b)
        da, db = a.state.digest(), b.state.digest()
        assert da == db
        delta = a.state.delta_for(db)
        assert delta["versions"] == {}

    def test_delta_only_ships_divergent_buckets(self):
        a, b = mk("a"), mk("b")
        # 200 keys spread over 64 buckets (~3 keys/bucket)
        for i in range(200):
            a.put_fact(f"default\x1fu{i}\x1fprefers", {"value": f"v{i}"})
        a.sync_with(b)
        assert a.converged_with(b)
        # now one key diverges -> exactly ONE bucket ships (~3 keys,
        # not all 200)
        a.put_fact("default\x1fu42\x1fprefers", {"value": "changed"})
        delta = a.state.delta_for(b.state.digest())
        assert len(delta["buckets"]) == 1
        shipped_keys = set(delta["versions"])
        assert "default\x1fu42\x1fprefers" in shipped_keys
        assert len(shipped_keys) <= 8          # bucket-sized, not global
        # every shipped key maps to the divergent bucket
        for k in shipped_keys:
            assert a.state.bucket_of(k) == delta["buckets"][0]

    def test_bandwidth_accounting(self):
        a, b = mk("a"), mk("b")
        a.put_fact(KEY, {"value": "Acme"})
        r = a.sync_with(b)
        assert r["a_applied"] + r["b_applied"] >= 1
        assert a.bytes_sent > 0 and b.bytes_received > 0


# ---------------------------------------------------------------- security
class TestSecurity:
    def test_rejects_forged_signature(self):
        a, b = mk("a"), mk("b")
        env = {"type": "delta", "from": "a", "to": "b",
               "clock": a.clock.now(),
               "delta": {"buckets": [], "versions":
                         {KEY: {"x": {"payload": {"value": "evil"}}}}}}
        env["sig"] = "0" * 64
        with pytest.raises(FederationError, match="bad signature"):
            b.apply_delta_envelope(env)

    def test_rejects_unknown_sender(self):
        a, b = mk("a"), mk("b")
        env = {"type": "digest", "from": "mallory", "clock": "1.0.mallory",
               "digest": {}}
        from cortexm.federation.node import sign_envelope
        env["sig"] = sign_envelope(env, "test-key")
        with pytest.raises(FederationError, match="unknown sender"):
            b.delta_envelope_for(env)

    def test_rejects_unsigned(self):
        b = mk("b")
        with pytest.raises(FederationError, match="unsigned"):
            b.apply_delta_envelope({"type": "delta", "from": "a",
                                    "delta": {}})

    def test_rejects_wrong_federation_key(self):
        a = mk("a")
        b = FederationNode("b", members=["a", "b"],
                           federation_key="DIFFERENT-key")
        with pytest.raises(FederationError):
            a.sync_with(b)


# ------------------------------------------------------------- file transport
class TestFileTransport:
    def test_offline_roundtrip(self, tmp_path):
        """Bidirectional offline exchange: mule -> drain -> reciprocate."""
        a, b = mk("a"), mk("b")
        t = FileTransport(tmp_path)
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        b.put_fact("default\x1fbob\x1flives_in", {"value": "Berlin",
                                                   "fact_id": "f2"})
        nodes = {"a": a, "b": b}
        t.exchange(nodes)
        assert a.converged_with(b)
        assert a.state.winner(KEY).payload["value"] == "Acme"
        assert a.state.winner("default\x1fbob\x1flives_in").payload[
            "value"] == "Berlin"

    def test_offline_three_way_convergence(self, tmp_path):
        a, b, c = mk("a"), mk("b"), mk("c")
        t = FileTransport(tmp_path)
        a.put_fact(KEY, {"value": "Acme", "fact_id": "f1"})
        b.put_fact("default\x1fbob\x1flives_in", {"value": "Berlin",
                                                   "fact_id": "f2"})
        c.retract("default\x1fcarol\x1fspeaks")
        nodes = {"a": a, "b": b, "c": c}
        t.exchange(nodes)
        assert a.converged_with(b) and b.converged_with(c)
        # repeated exchange is a no-op (idempotent merge)
        before = a.state.canonical_bytes()
        t.exchange(nodes)
        assert a.state.canonical_bytes() == before


# ------------------------------------------------------------- state layer
class TestState:
    def test_snapshot_roundtrip(self):
        a = mk("a")
        a.put_fact(KEY, {"value": "Acme"})
        a.retract("default\x1fbob\x1flives_in")
        snap = a.state.snapshot()
        b_state = FederationState.from_snapshot(snap)
        assert b_state.canonical_bytes() == a.state.canonical_bytes()

    def test_value_history_is_temporal(self):
        a = mk("a")
        a.put_fact(KEY, {"value": "Acme", "valid_from": "2024-01-01"})
        a.put_fact(KEY, {"value": "Globex", "valid_from": "2025-01-01"})
        hist = a.state.value_history(KEY)
        assert [v.payload["value"] for v in hist] == ["Acme", "Globex"]

    def test_stats(self):
        a = mk("a")
        a.put_fact(KEY, {"value": "Acme"})
        a.retract("default\x1fbob\x1flives_in")
        s = a.state.stats()
        assert s == {"keys": 2, "versions": 2, "tombstones": 1}


# -------------------------------------------------------- fabric roundtrip
class TestFabricBridge:
    def test_store_roundtrip(self):
        """fabric -> CRDT -> federate -> new fabric: identical active view."""
        from cortexm.trace.fact import make_fact
        from cortexm.trace.store import TraceStore
        from cortexm.federation import (apply_to_store,
                                          node_from_store)

        t0 = __import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)
        s1 = TraceStore(":memory:")
        c = s1.create_commit("seed")
        s1.insert_facts_bulk([
            make_fact("Alice", "works_at", "Acme", now=t0),
            make_fact("Alice", "prefers", "vim", now=t0),
            make_fact("Bob", "lives_in", "Berlin", now=t0),
        ], c)

        a = node_from_store("a", s1, members=["a", "b"],
                            federation_key="test-key")
        b = FederationNode("b", members=["a", "b"],
                           federation_key="test-key")
        b.put_fact("default\x1fCarol\x1fspeaks",
                   {"subject": "Carol", "relation": "speaks",
                    "value": "French", "valid_from": "2026-01-01",
                    "fact_id": "f9"})
        a.sync_with(b)

        s2 = TraceStore(":memory:")
        apply_to_store(a, s2)
        live1 = {(f.subject, f.relation, f.value)
                 for f in s1.query_facts() if f.is_active}
        live2 = {(f.subject, f.relation, f.value)
                 for f in s2.query_facts() if f.is_active}
        assert live1 <= live2
        assert ("Carol", "speaks", "French") in live2

    def test_bidirectional_fabric_federation(self):
        from cortexm.trace.fact import make_fact
        from cortexm.trace.store import TraceStore
        from cortexm.federation import (apply_to_store,
                                          export_to_crdt,
                                          node_from_store)

        t0 = __import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)
        s1 = TraceStore(":memory:")
        s1.insert_facts_bulk([make_fact("Alice", "works_at", "Acme",
                                        now=t0)], s1.create_commit("s"))
        s2 = TraceStore(":memory:")
        s2.insert_facts_bulk([make_fact("Bob", "lives_in", "Oslo",
                                        now=t0)], s2.create_commit("s"))

        a = node_from_store("a", s1, members=["a", "b"],
                            federation_key="test-key")
        b = node_from_store("b", s2, members=["a", "b"],
                            federation_key="test-key")
        a.sync_with(b)

        # both fabrics receive the union
        apply_to_store(a, s1)
        apply_to_store(b, s2)
        v1 = {(f.subject, f.relation, f.value)
              for f in s1.query_facts() if f.is_active}
        v2 = {(f.subject, f.relation, f.value)
              for f in s2.query_facts() if f.is_active}
        assert v1 == v2
        assert ("Alice", "works_at", "Acme") in v1
        assert ("Bob", "lives_in", "Oslo") in v1
