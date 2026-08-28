"""1 — Quickstart: add, search, μ=0. Offline, no API keys."""
from cortexm import Memory

m = Memory()
m.add("My name is Alice Johnson. I work at Google as a software engineer.",
      user_id="alice")
m.add("I live in Toronto and I prefer oat milk lattes.", user_id="alice")

out = m.search("Where does Alice live?", user_id="alice")
print(out["context_block"])
print("\nLLM calls used:", out["llm_calls"], "(μ=0 protocol)")
m.close()
