"""Example 14 — CRDT federation: two fabrics, one converged memory.

What this shows:
  * node A and node B each ingest DIFFERENT conversations locally (μ=0);
  * a partition-tolerant sync exchanges HMAC-signed digest/delta
    envelopes (here over an in-memory mesh; the file transport does the
    same offline via outbox/inbox spool dirs);
  * both fabrics converge to the byte-exact same memory state —
    no coordinator, no locks, delivery order irrelevant;
  * a retraction on one side propagates and survives the merge.

Requires nothing beyond the core package (pure Python CRDT).
"""

import datetime as dt

from cortexm import Memory
from cortexm.federation import (
    InMemoryMesh,
    apply_to_store,
    node_from_store,
)

TS = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)

# --- two independent fabrics (imagine two edge devices) --------------------
a = Memory()
a.add([{"role": "user", "content":
        "I'm Alice. I work at Acme Corp on the storage team, "
        "and we deploy everything on Kubernetes.", "timestamp": TS}])

b = Memory()
b.add([{"role": "user", "content":
        "Bob here — I live in Oslo, prefer Rust over C++, "
        "and my team runs Postgres 16 in production.", "timestamp": TS}])

# --- wrap both in federation nodes ------------------------------------------
na = node_from_store("edge-a", a.store, members=["edge-a", "edge-b"],
                     federation_key="demo-key")
nb = node_from_store("edge-b", b.store, members=["edge-a", "edge-b"],
                     federation_key="demo-key")

print("before sync:",
      "A knows", len(a.store.active_facts()), "facts |",
      "B knows", len(b.store.active_facts()), "facts")

# --- sync (in-memory mesh; FileTransport does the same offline) ------------
mesh = InMemoryMesh()
mesh.link("edge-a", "edge-b")
mesh.gossip({"edge-a": na, "edge-b": nb}, rounds=2)
print("byte-exact convergence:", na.converged_with(nb))

# --- write the converged state back into both fabrics -----------------------
apply_to_store(na, a.store)
apply_to_store(nb, b.store)
va = {(f.subject, f.relation, f.value)
      for f in a.store.active_facts()}
vb = {(f.subject, f.relation, f.value)
      for f in b.store.active_facts()}
print("both fabrics see the same world:", va == vb)
print("  A's view:", sorted(va)[:4], "...")

# --- a GDPR-style retraction on one side, then re-sync ----------------------
node_a2 = node_from_store("edge-a", a.store, members=["edge-a", "edge-b"],
                          federation_key="demo-key")
node_b2 = node_from_store("edge-b", b.store, members=["edge-a", "edge-b"],
                          federation_key="demo-key")
for f in node_a2.state.keys():
    if "\x1falice\x1f" in f.lower():
        node_a2.retract(f, purge=True)          # poison-pill erase
node_a2.sync_with(node_b2)
print("after purge sync, converged:", node_a2.converged_with(node_b2))

a.close()
b.close()
print("\nnext: context_m/federation/transport.py FileTransport — the same")
print("envelopes as JSONL spool files your rsync/git/USB 'mule' moves.")
