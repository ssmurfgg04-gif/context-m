"""2 — Mem0-compatible drop-in: same API surface, neuro-symbolic core."""
from context_m import Memory

m = Memory()
m.add([{"role": "user", "content": "I work at Google"},
       {"role": "user", "content": "I live in Toronto"}], user_id="alice")
results = m.search("Where does Alice work?", user_id="alice")["results"]
print("search:", [r["memory"] for r in results][:3])
print("get_all:", len(m.get_all(user_id="alice")["results"]), "memories")
first = m.get_all(user_id="alice")["results"][0]
print("history of", first["id"][:8], ":", m.history(first["id"]))
m.close()
