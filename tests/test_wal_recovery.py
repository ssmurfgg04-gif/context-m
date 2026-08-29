"""WAL crash-recovery guarantees (Aeon-inspired durability proof).

Simulates the failure mode the strategic plan calls out: a hard crash
mid-write must never lose COMMITTED memories or corrupt the store. A
subprocess ingests batches and is SIGKILLed without warning; the reopened
database must (a) pass SQLite integrity checking, (b) contain the state of
the LAST acknowledged batch (supersession semantics mean older name/city
facts are intentionally retired — the surviving state is what matters),
and (c) accept new writes.
"""

import datetime as dt
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from cortexm import Memory
from cortexm.config import Config

TS = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)

WORKER = textwrap.dedent("""
    import datetime as dt, sys
    sys.path.insert(0, {repo!r})
    from cortexm import Memory
    from cortexm.config import Config

    NAMES = ["Alice", "Boris", "Carla", "Dev", "Elena", "Farid",
             "Gina", "Hugo", "Ines", "Jamal", "Katya", "Liam",
             "Mona", "Nils", "Olga", "Pavel", "Quinn", "Rosa",
             "Stefan", "Tara", "Umar", "Vera", "Wes", "Xena",
             "Yusuf", "Zara"]
    CITIES = ["Osaka", "Prague", "Quito", "Rome", "Seoul", "Tunis",
              "Uppsala", "Valpo", "Warsaw", "Xian", "York", "Zurich",
              "Ajaccio", "Bergen", "Cusco", "Djibou", "Evora", "Fes",
              "Genoa", "Hanoi", "Ibiza", "Jakarta", "Kyoto", "Lima",
              "Milan", "Naples"]

    db, ack_file = sys.argv[1], sys.argv[2]
    m = Memory(Config(db_path=db))
    for i in range(500):
        m.add([{{"role": "user",
                "content": f"Update: my name is {{NAMES[i % 26]}} and "
                           f"I live in {{CITIES[i % 26]}}."}}],
              user_id="crash", timestamp=dt.datetime(2026, 1, 10,
                                                     tzinfo=dt.timezone.utc))
        with open(ack_file, "w") as fh:   # ack AFTER the add() returned
            fh.write(str(i))
    m.close()
""")


def test_wal_survives_sigkill(tmp_path):
    if not hasattr(signal, "SIGKILL"):
        pytest.skip("SIGKILL not available on this platform (Windows)")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = str(tmp_path / "crash.db")
    ack = str(tmp_path / "acked.txt")
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER.format(repo=repo))

    proc = subprocess.Popen([sys.executable, str(worker), db, ack],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    last_acked = -1
    deadline = time.time() + 30
    while time.time() < deadline:
        if os.path.exists(ack):
            try:
                last_acked = int(open(ack).read().strip())
            except ValueError:
                pass
        if last_acked >= 8:
            break
        time.sleep(0.02)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert last_acked >= 8, "worker never reached a committed state"

    # reopen: integrity + last acknowledged state present. The kill can land
    # one batch AFTER the ack was written, so accept {last, last+1}.
    NAMES = ["Alice", "Boris", "Carla", "Dev", "Elena", "Farid",
             "Gina", "Hugo", "Ines", "Jamal", "Katya", "Liam",
             "Mona", "Nils", "Olga", "Pavel", "Quinn", "Rosa",
             "Stefan", "Tara", "Umar", "Vera", "Wes", "Xena",
             "Yusuf", "Zara"]
    CITIES = ["Osaka", "Prague", "Quito", "Rome", "Seoul", "Tunis",
              "Uppsala", "Valpo", "Warsaw", "Xian", "York", "Zurich",
              "Ajaccio", "Bergen", "Cusco", "Djibou", "Evora", "Fes",
              "Genoa", "Hanoi", "Ibiza", "Jakarta", "Kyoto", "Lima",
              "Milan", "Naples"]
    m = Memory(Config(db_path=db))
    view = m.search("What is the user's name? Where do they live?",
                    user_id="crash")["context_block"].lower()
    ok_names = {NAMES[last_acked % 26].lower(), NAMES[(last_acked + 1) % 26].lower()}
    ok_cities = {CITIES[last_acked % 26].lower(), CITIES[(last_acked + 1) % 26].lower()}
    assert any(n in view for n in ok_names), (
        f"last committed name lost after SIGKILL (want one of {ok_names})")
    assert any(c in view for c in ok_cities), (
        f"last committed city lost after SIGKILL (want one of {ok_cities})")

    # store still writable after recovery
    m.add("Post-crash write: I work at Stripe as an engineer.",
          user_id="crash", timestamp=TS)
    assert "stripe" in m.search("Where does the user work?",
                                user_id="crash")["context_block"].lower()
    m.close()

    # SQLite's own integrity check must pass
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_wal_sync_full_option(tmp_path):
    db = str(tmp_path / "sync.db")
    m = Memory(Config(db_path=db, wal_sync="full"))
    m.add("I work at Google as an engineer.", user_id="x", timestamp=TS)
    m.close()
    import sqlite3
    conn = sqlite3.connect(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"
    assert sync == 2  # FULL


def test_close_checkpoints_wal(tmp_path):
    db = str(tmp_path / "cp.db")
    m = Memory(Config(db_path=db))
    m.add("I work at Google as an engineer.", user_id="x", timestamp=TS)
    m.close()
    # after a clean close the WAL is folded back (truncated to zero)
    wal = db + "-wal"
    assert not os.path.exists(wal) or os.path.getsize(wal) == 0
