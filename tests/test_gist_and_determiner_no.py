"""Gist fallback + determiner-no tests (v0.6.8 learning-loop fixes).

- Pattern-less prose must still leave an indexable fact (never silently
  dropped), running on the FULL message text so negated sentences leave
  a retrievable gist record.
- Bare determiner-"no" ("pros and no cons") stays in positive text but
  is still recorded; verb-adjacent "no" ("eat no meat") and "don't"
  still strip (v0.6.1 regression guard).
"""
from __future__ import annotations

from cortexm.api.memory import Memory
from cortexm.bridge.negation import detect_negation, extract_with_negation


def _mem():
    return Memory(db_path=":memory:")


def test_gist_fallback_emits_noted_fact() -> None:
    mem = _mem()
    mem.add("The office kitchen has a standing desk policy.", user_id="probe")
    facts = mem.get_all(user_id="probe", limit=50)["results"]
    gists = [f for f in facts if "gist_fallback" in f["memory"]
             or "| noted |" in f["memory"]]
    assert gists, f"no gist fact emitted: {[f['memory'] for f in facts]}"
    assert float(gists[0]["confidence"]) >= 0.30


def test_gist_fallback_runs_on_full_negated_text() -> None:
    mem = _mem()
    mem.add("Ship the release with solid docs and no regressions.",
            user_id="probe")
    facts = mem.get_all(user_id="probe", limit=50)["results"]
    assert any("| noted |" in f["memory"] for f in facts), \
        f"negated lesson left no gist: {[f['memory'] for f in facts]}"
    recs = mem.store.query_negation_records(user_id="probe")
    assert recs, "determiner-no denial was not recorded"


def test_gist_fallback_skips_short_text() -> None:
    mem = _mem()
    mem.add("ok", user_id="probe")
    facts = mem.get_all(user_id="probe", limit=50)["results"]
    assert not any("| noted |" in f["memory"] for f in facts)


def test_gist_fallback_disabled_by_config() -> None:
    mem = _mem()
    mem.config.gist_fallback_enabled = False
    mem.add("The office kitchen has a standing desk policy.", user_id="probe")
    facts = mem.get_all(user_id="probe", limit=50)["results"]
    assert not any("| noted |" in f["memory"] for f in facts)


def test_dont_still_strips() -> None:
    rows = detect_negation("I don't eat meat.")
    assert rows and not rows[0].get("determiner")
    split = extract_with_negation("I don't eat meat.")
    assert "eat meat" not in split["positive_text"]


def test_verb_adjacent_no_still_strips() -> None:
    rows = detect_negation("I eat no meat.")
    assert rows and not rows[0].get("determiner")
    split = extract_with_negation("I eat no meat.")
    assert "eat no meat" not in split["positive_text"]


def test_determiner_no_stays_but_recorded() -> None:
    rows = detect_negation("Pros and no cons were listed.")
    assert rows and rows[0].get("determiner") is True
    split = extract_with_negation("Pros and no cons were listed.")
    assert "no cons" in split["positive_text"]
    assert len(split["negations"]) == 1


def test_sentence_initial_no_still_strips() -> None:
    rows = detect_negation("No fish here, only meat.")
    assert rows and not rows[0].get("determiner")
    split = extract_with_negation("No fish here, only meat.")
    assert "No fish here" not in split["positive_text"]


def test_pseudo_negation_not_only_stays() -> None:
    rows = detect_negation("Learning is not only about grades.")
    assert rows == []
    split = extract_with_negation("Learning is not only about grades.")
    assert "not only" in split["positive_text"]
    assert split["negations"] == []


def test_pseudo_negation_not_necessarily_stays() -> None:
    rows = detect_negation("It is not necessarily true.")
    assert rows == []


def test_pseudo_does_not_mask_real_negation() -> None:
    # A real marker OUTSIDE the pseudo span still fires.
    rows = detect_negation("It is not only late, it never arrived.")
    assert rows and rows[0]["marker"].lower() == "never"