"""3 — Zep-compatible bi-temporal queries: valid time vs transaction time."""
import datetime as dt
from cortexm import Memory

m = Memory()
t0 = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
m.add("I work at Google. I live in Toronto.", user_id="alice", timestamp=t0)
m.add("I left Google in March. I joined Anthropic in April 2025.",
      user_id="alice", timestamp=dt.datetime(2025, 5, 1, tzinfo=dt.timezone.utc))

print("valid in 2024 (reality):",
      [f["fact"] for f in m.get_between("2024-01-01", "2024-12-31",
                                        user_id="alice") if "works_at" in f["fact"]])
print("after 2025-03:",
      [f["fact"] for f in m.get_after("2025-03-01", user_id="alice")
       if "works_at" in f["fact"]])
m.close()
