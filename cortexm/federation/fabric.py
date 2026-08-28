"""Bridge the local TraceStore (symbolic layer) into the federated CRDT.

Direction 1 — ``export_to_crdt``: every stored fact (active AND historical)
becomes an immutable CRDT version under its key. SINGLE_VALUED relations
collapse into one versioned register per (user, subject, relation);
MULTI_VALUED facts get one register per distinct value.

Direction 2 — ``apply_to_store``: merge a node's converged state back into
a fabric: unknown fact ids are inserted, known ids are updated field-wise
(stamps guarantee newest-wins deterministically). The fabric's own truth
maintenance (SUPERSEDE edges) is NOT replayed — the CRDT versions already
carry the bi-temporal windows, so the store receives resolved history.

Caveat (honest scope): retrieval-side caches (VSA palace projections) must
be rebuilt after applying remote deltas — ``apply_to_store`` returns the
number of store writes so callers can decide when to reproject.
"""

from __future__ import annotations

from cortexm.federation.crdt import (
    FederationState,
    fact_key,
)
from cortexm.federation.hlc import HLC
from cortexm.federation.node import FederationNode
from cortexm.trace.fact import Fact, SINGLE_VALUED

_FED_FIELDS = (
    "subject", "relation", "value", "valid_from", "valid_to",
    "confidence", "user_id", "agent_id", "run_id", "memory_type",
    "source_hash", "source_id", "provenance",
)


def _fact_payload(f: Fact) -> dict:
    return {k: getattr(f, k) for k in _FED_FIELDS}


def export_to_crdt(store, node: FederationNode,
                   user_id: str | None = None) -> int:
    """Push the store's facts into the node's CRDT state."""
    facts = store.query_facts(user_id=user_id)   # active + historical
    n = 0
    for f in facts:
        single = f.relation in SINGLE_VALUED
        key = fact_key(f.user_id, f.subject, f.relation,
                       value=f.value, single_valued=single)
        payload = _fact_payload(f)
        payload["fact_id"] = f.id
        if not f.is_active:
            # inactive fact -> tombstone version carrying its own payload
            # so history queries can still see WHAT was retracted
            node.state.put(key, payload, node.clock, tombstone=True)
        else:
            node.state.put(key, payload, node.clock)
        n += 1
    return n


def apply_to_store(node: FederationNode, store) -> dict:
    """Pull the node's converged CRDT state into a store."""
    inserted = updated = retracted = 0
    for key in node.state.keys():
        live = node.state.value_history(key)
        for v in live:
            payload = v.payload
            fact_id = payload.get("fact_id")
            required = ("subject", "relation", "value", "valid_from")
            if not fact_id or any(k not in payload for k in required):
                continue          # incomplete version — not fabric-shaped
            existing = store.get_fact(fact_id)
            if existing is None:
                f = Fact(id=fact_id, **{k: payload[k] for k in _FED_FIELDS
                                        if k in payload})
                store.insert_fact(f)
                inserted += 1
            else:
                dirty = {k: payload[k] for k in _FED_FIELDS
                         if k in payload and getattr(existing, k) !=
                         payload[k]}
                if dirty:
                    store.update_fact(fact_id, **dirty)
                    updated += 1
        # retracted keys: mirror tombstones into the store
        all_v = node.state.all_versions(key)
        tombstoned_ids = {v.payload.get("fact_id") for v in all_v
                          if v.tombstone and v.payload}
        for fid in tombstoned_ids:
            if fid and store.get_fact(fid) is not None:
                f = store.get_fact(fid)
                if f.is_active:
                    store.update_fact(fid, is_active=False)
                    retracted += 1
    return {"inserted": inserted, "updated": updated,
            "retracted": retracted}


def node_from_store(node_id: str, store, members: list[str] | None = None,
                    federation_key: str = "default-fed-key",
                    user_id: str | None = None) -> FederationNode:
    node = FederationNode(node_id, members=members,
                          federation_key=federation_key)
    export_to_crdt(store, node, user_id=user_id)
    return node


__all__ = ["export_to_crdt", "apply_to_store", "node_from_store",
           "FederationState", "FederationNode", "HLC", "fact_key"]
