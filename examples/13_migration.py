"""Example 13 — one-command migration off the incumbents.

The Trojan-Horse tooling from the strategic plan: read a competitor's
local store and convert it into neuro-symbolic memory, μ=0 re-extraction
on raw text where possible, timestamps preserved.
"""

import datetime as dt
import json
import sqlite3
import tempfile
from pathlib import Path

from context_m import Memory

TS = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
td = Path(tempfile.mkdtemp())

# --- build a realistic mem0 store (history table, JSON payloads) -----------
mem0_db = td / "mem0.db"
conn = sqlite3.connect(mem0_db)
conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, data TEXT, "
             "created_at TEXT, updated_at TEXT)")
conn.execute("INSERT INTO history(data, created_at, updated_at) VALUES(?,?,?)",
             (json.dumps({
                 "messages": [
                     {"role": "user", "content": "My name is Marco Silva."},
                     {"role": "user",
                      "content": "I work at Shopify as a data engineer."}],
                 "memories": ["Name is Marco Silva"],
             }), "2026-02-01T10:00:00", "2026-02-01T10:00:01"))
conn.execute("INSERT INTO history(data, created_at, updated_at) VALUES(?,?,?)",
             (json.dumps({
                 "messages": [{"role": "user",
                              "content": "I moved to Porto in March."}],
             }), "2026-03-05T09:00:00", "2026-03-05T09:00:01"))
conn.commit()
conn.close()

# --- migrate ----------------------------------------------------------------
from context_m.migrate.importers import import_mem0

m = Memory()
report = import_mem0(m, str(mem0_db), user_id="marco")
print("migration report:", report)

for q in ("What is Marco's name?", "Where does Marco work?",
          "Where does Marco live?"):
    out = m.search(q, user_id="marco")
    line = out["context_block"].splitlines()
    print(f"{q:28s} ->", line[1][:65] if len(line) > 1 else "(nothing)")

# --- the CLI equivalent ------------------------------------------------------
#   cortexm migrate --from mem0 --path mem0.db --user-id marco
#   cortexm migrate --from zep   --path zep_export.jsonl
#   cortexm migrate --from chroma --path chroma.sqlite3
print("\nCLI: cortexm migrate --from mem0 --path <store>")
