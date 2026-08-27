"""7 — Memory Git: branch an agent personality, merge, diff, blame."""
import datetime as dt
from context_m import Memory

m = Memory()
m.add("I prefer concise answers.", user_id="dev",
      timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
m.branch("experiment")
m.add("I prefer verbose answers with citations.", user_id="dev",
      timestamp=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
m.checkout("main")
main_head = m.store.head()
m.checkout("experiment")
exp_head = m.store.head()
print("diff main→experiment:", m.diff(main_head, exp_head)["n_added"], "added")
m.checkout("main")
print("merge:", m.merge("experiment")["status"])
for row in m.blame("dev", "prefers")[:3]:
    print(" blame:", row["fact"], "| active:", row["active"])
m.close()
