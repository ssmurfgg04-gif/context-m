"""9 — Self-healing memory: bit flips detected, TMR majority vote, re-encode."""
from context_m.config import Config
from context_m import Memory

m = Memory(Config(codec="binary", tmr=True))
m.add("My name is Ada Lovelace. I work on the Analytical Engine.",
      user_id="ada")
flipped = m.corrupt(rate=0.05, seed=1)
health = m.health_check()
print(f"injected bit flips into {flipped} vectors; "
      f"corrupt detected: {health['corrupt']}")
print("heal:", m.heal())
print("verify:", m.verify_integrity()["ok"])
m.close()
