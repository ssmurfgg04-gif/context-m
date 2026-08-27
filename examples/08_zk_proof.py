"""8 — Zero-knowledge-lite: prove a memory matches without revealing it."""
from context_m import Memory

m = Memory()
m.add("My name is Grace. My allergy is penicillin — confidential record.",
      user_id="grace")
proof = m.prove("What is Grace's allergy?", user_id="grace")
print("LLM view:", proof["llm_view"])
print("statement:", proof["statement"][:80], "…")
print("verified:", m.verify_proof(proof))
m.close()
