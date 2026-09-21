"""Session lifecycle tests (v0.6.8): start/note/end + handoff brief."""
from __future__ import annotations

from cortexm.api.memory import Memory


def _mem():
    return Memory(db_path=":memory:")


def test_session_start_mints_run_and_briefs() -> None:
    mem = _mem()
    mem.add("Alice works at Google.", user_id="probe")
    out = mem.session_start(user_id="probe")
    assert out["event"] == "SESSION_START"
    assert out["run_id"].startswith("sess-")
    assert "Google" in out["briefing"]


def test_session_start_honors_given_run() -> None:
    mem = _mem()
    out = mem.session_start(user_id="probe", run_id="run-1")
    assert out["run_id"] == "run-1"


def test_session_note_and_end_handoff() -> None:
    mem = _mem()
    mem.session_start(user_id="probe", run_id="run-9")
    mem.session_note("Decided to ship Friday.", user_id="probe",
                     run_id="run-9", kind="decision")
    mem.session_note("Always confirm the deploy window first.",
                     user_id="probe", run_id="run-9", kind="lesson")
    out = mem.session_end(user_id="probe", run_id="run-9")
    assert out["event"] == "SESSION_END"
    assert "run-9" in out["handoff"]
    assert "Friday" in out["handoff"]
    assert out["facts"] >= 1


def test_session_end_empty_run_graceful() -> None:
    mem = _mem()
    out = mem.session_end(user_id="probe", run_id="run-empty")
    assert "No observations" in out["handoff"]
    assert out["facts"] == 0


def test_session_note_short_text_still_recorded() -> None:
    # Explicit capture always leaves a record, even below the
    # ambient gist_min_chars floor ("Ship Friday." = 12 chars).
    mem = _mem()
    out = mem.session_note("Ship Friday.", user_id="probe",
                           run_id="run-s", kind="decision")
    assert out["facts"] >= 1
    end = mem.session_end(user_id="probe", run_id="run-s")
    assert "Friday" in end["handoff"]