"""Skill-pattern expansion tests (v0.6.8).

Covers the "has skill" blind spot: first-person prep forms, "my
skills include", "skilled/proficient at", and third-person "Dave
has skill rust" (normalized to has_skill via SUMMARY_VERBS) —
plus false-positive guards.
"""
from __future__ import annotations

from cortexm.api.memory import Memory


def _skills(mem, user_id="probe"):
    return [f for f in mem.get_all(user_id=user_id, limit=100)["results"]
            if "| has_skill |" in f["memory"]]


def _mem(msgs, user_id="probe"):
    mem = Memory(db_path=":memory:")
    for m in msgs:
        mem.add(m, user_id=user_id)
    return mem


def test_have_skill_prep_form() -> None:
    mem = _mem(["I have skill in rust."])
    vals = [f["memory"] for f in _skills(mem)]
    assert any("rust" in v for v in vals), vals


def test_have_skills_list_form() -> None:
    mem = _mem(["I have skills with python and go."])
    vals = [f["memory"] for f in _skills(mem)]
    assert any("python" in v for v in vals), vals
    assert any("| go" in v or " go" in v for v in vals), vals


def test_my_skills_include() -> None:
    mem = _mem(["My skills include welding."])
    vals = [f["memory"] for f in _skills(mem)]
    assert any("welding" in v for v in vals), vals


def test_third_person_has_skill() -> None:
    mem = _mem(["Dave has skill rust."])
    vals = [f["memory"] for f in _skills(mem)]
    assert any("Dave" in v and "rust" in v for v in vals), vals


def test_skilled_proficient_at() -> None:
    mem = _mem(["I am skilled at welding.", "I'm proficient at hiring."])
    vals = [f["memory"] for f in _skills(mem)]
    assert any("welding" in v for v in vals), vals
    assert any("hiring" in v for v in vals), vals


def test_no_fire_on_skill_issues() -> None:
    mem = _mem(["I have skill issues."])
    vals = [f["memory"] for f in _skills(mem)]
    assert not any("issues" in v for v in vals), vals


def test_no_fire_on_multitail_include() -> None:
    mem = _mem(["My skills include paying bills on time."])
    assert _skills(mem) == []