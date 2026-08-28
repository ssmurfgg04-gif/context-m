"""5 — Truth maintenance: supersessions with full history."""
import datetime as dt
from cortexm import Memory

m = Memory()
m.add("I work at Stripe.", user_id="sam",
      timestamp=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc))
m.add("I left Stripe in June. These days I work at Ramp.",
      user_id="sam", timestamp=dt.datetime(2025, 7, 1, tzinfo=dt.timezone.utc))

out = m.search("Where does Sam work now?", user_id="sam")
print(out["context_block"][:600])
m.close()
