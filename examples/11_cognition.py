"""Example 11 — Cognition Engine: hypothesis from kinship facts.

Demonstrates the HMS-style Cognition Engine. The engine is the standout
feature of holographic-memory (HMS): it turns a passive store into a
self-organizing knowledge base. When you say "Alice's father is Bob"
and later "Bob's father is Charles," the engine hypothesizes that
Alice's grandfather (father*father) is Charles — and writes this as a
HYPOTHESIZED_BY edge in the Trace, with confidence < 0.5 so it's never
active in retrieval unless explicitly confirmed.

This is the category-defining feature that Mem0, Zep, and Letta do
not have. It turns Context-M from "memory database" to "self-organizing
memory brain."

Run:
    python examples/11_cognition.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from context_m.api.memory import Memory
from context_m.config import Config
from context_m.trace.fact import Fact
from context_m.util import new_id, iso


def _add_fact(store, subject, relation, value, user_id="alice"):
    """Insert a fact directly (bypassing the extractor)."""
    fact = Fact(
        id=new_id(),
        subject=subject, relation=relation, value=value,
        valid_from=iso(datetime.now(timezone.utc)),
        confidence=0.85, user_id=user_id,
        memory_type="long_term",
    )
    store.insert_fact(fact)
    store._maybe_commit()
    return fact.id


def main():
    print("=" * 72)
    print("Context-M Cognition Engine — HMS-style self-organization")
    print("=" * 72)
    print()

    cfg = Config(db_path=":memory:", cognition_enabled=True,
                 fade_enabled=False, tmt_enabled=False)
    mem = Memory(cfg)

    # 1. Seed the trace with two father-facts
    print("[1] Seeding the Trace with kinship facts:")
    print("    (Alice, father, Bob)")
    print("    (Bob, father, Charles)")
    print("    (Charles, father, David)")
    print()
    _add_fact(mem.store, "Alice", "father", "Bob")
    _add_fact(mem.store, "Bob", "father", "Charles")
    _add_fact(mem.store, "Charles", "father", "David")

    # 2. Run the cognition pass
    print("[2] Running the 5-stage cognition pass:")
    print("    PatternScanner -> AbstractionEngine -> GapDetector")
    print("    -> HypothesisEngine -> AnalogyDetector")
    print()
    from context_m.cognition import run_cognition_pass
    report = run_cognition_pass(mem.store, palace=mem.palace)
    print(f"    scan: {report.scan['patterns']} patterns surfaced "
          f"({report.scan['n_facts_scanned']} facts scanned)")
    print(f"    abstraction: {report.abstraction['abstractions']} "
          f"prototype categories built "
          f"({report.abstraction['membership_edges_added']} edges)")
    print(f"    gaps: {report.gaps['gaps']} missing relations found")
    print(f"    hypotheses: {report.hypotheses['facts_added']} proposed")
    print(f"    analogies: {report.analogies['analogies']} structural "
          f"isomorphisms")
    print()

    # 3. Inspect the hypothesis fact(s)
    print("[3] Hypothesis facts written to the Trace:")
    rows = mem.store.conn.execute(
        "SELECT subject, relation, value, confidence, provenance "
        "FROM facts WHERE is_derived=1 AND "
        "provenance LIKE '%\"kind\": \"hypothesis\"%'").fetchall()
    for r in rows:
        print(f"    ({r[0]}, {r[1]}, {r[2]})  conf={r[3]:.3f}")
        # pull the supporting fact IDs from provenance
        import json
        prov = json.loads(r[4])
        supporting = prov.get("supporting_facts", [])
        print(f"        basis: {prov.get('basis')}")
        print(f"        supporting fact IDs: "
              f"{[sid[:8] + '...' for sid in supporting[:3]]}")
    print()

    # 4. Inspect the HYPOTHESIZED_BY edges
    print("[4] HYPOTHESIZED_BY edges in the Trace:")
    rows = mem.store.conn.execute(
        "SELECT src, dst FROM edges WHERE kind='HYPOTHESIZED_BY'").fetchall()
    for r in rows:
        print(f"    {r[0][:8]}... HYPOTHESIZED_BY {r[1][:8]}...")
    print(f"    Total: {len(rows)} edges")
    print()

    # 5. Test the structural multi-hop query
    print("[5] Structural query — answer 'Who is Alice's grandfather?'")
    print("    structural_query(Alice, [father, father])")
    from context_m.trace.structural import structural_query
    res = structural_query(
        mem.store, mem.palace,
        start_entity="Alice",
        relation_chain=["father", "father"],
        user_id="alice")
    print(f"    success: {res.success}")
    print(f"    final_value: {res.final_value}")
    print(f"    confidence: {res.confidence:.3f}")
    print(f"    hops:")
    for i, hop in enumerate(res.hops, 1):
        print(f"      hop {i}: {hop.subject} --{hop.relation}--> "
              f"{hop.value} (via {hop.via}, conf={hop.confidence:.3f})")
    print()

    # 6. Note that hypotheses are never active in retrieval
    print("[6] Confirm hypotheses don't pollute active retrieval:")
    rows = mem.store.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE is_derived=1 "
        "AND provenance LIKE '%\"kind\": \"hypothesis\"%'").fetchone()
    print(f"    Derived hypothesis facts in trace: {rows[0]}")
    rows = mem.store.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE is_active=1 "
        "AND is_derived=0").fetchone()
    print(f"    Active (non-derived) facts in trace: {rows[0]}")
    print("    (Hypotheses are is_derived=1, excluded by default search)")
    print()

    print("=" * 72)
    print("Cognition Engine demonstration complete.")
    print("The Trace now self-organizes — turning Context-M from a")
    print("memory database into a self-organizing memory brain.")
    print("=" * 72)


if __name__ == "__main__":
    main()
