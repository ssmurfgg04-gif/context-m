"""CLI session commands (v0.6.8 hook surface): start/note/end round-trip."""
from __future__ import annotations

import json

from cortexm.cli import main


def test_cli_session_roundtrip(tmp_path, capsys) -> None:
    db = str(tmp_path / "s.db")
    assert main(["session-start", "--db", db, "--user-id", "cli",
                 "--run-id", "r1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == "r1"
    assert main(["session-note", "Decided to ship Friday.", "--db", db,
                 "--user-id", "cli", "--run-id", "r1",
                 "--kind", "decision"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["event"] == "SESSION_NOTE"
    assert main(["session-end", "--db", db, "--user-id", "cli",
                 "--run-id", "r1"]) == 0
    handoff = capsys.readouterr().out
    assert "r1" in handoff and "Friday" in handoff


def test_cli_session_start_mints_run(tmp_path, capsys) -> None:
    db = str(tmp_path / "s.db")
    assert main(["session-start", "--db", db, "--user-id", "cli"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"].startswith("sess-")