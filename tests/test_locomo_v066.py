"""Tests for the v0.6.6 LoCoMo canonical runner machinery.

Covers the deterministic pieces that produced the measured LoCoMo
number (mirroring the LongMemEval test culture: every judge addition
gets unit tests so the boring fixes stay boring):
  - relative-time derivation ("last year", "for 10 years", ...)
  - absolute-date anchor parsing ("in May 2023", "between A and B")
  - the when-date judge (year / full date / weekday-relative)
  - number-word nugget normalization ("six months" vs "6 months")
  - the adversarial (speaker-swap trap) rubric
  - top-5 budget extraction (prefers the ranked verbatim section)
  - the LoCoMo date parser and flatten shape
  - an end-to-end smoke through run_locomo_conversation
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.locomo_canonical import (
    _derive_evidence_lines, _parse_absolute_anchors, _bare_month_anchor,
    _judge_when, _judge_numberword_nugget, _numberword_normalize,
    adversarial_correct, top5_block_from_cb, parse_locomo_date,
    flatten_locomo_conversation, run_locomo_conversation,
    locomo_judge, summarize,
)

TS = datetime(2023, 5, 8, tzinfo=timezone.utc)


# ------------------------- derivation rules -------------------------------

@pytest.mark.parametrize("text,expected", [
    ("I painted that lake sunrise last year!", ("'last year'", "2022")),
    ("we had to say goodbye to Max for 10 years",
     ("'for 10 years'", "2013")),
    ("I started surfing five years ago", ("'five years ago'", "2018")),
    ("I've been at it three years back", ("'three years back'", "2020")),
    ("I went to the group yesterday", ("'yesterday'", "2023-05-07")),
    ("I have been dancing since 2016", ("'since 2016'", "2016")),
    ("we adopted him back in 2019", ("'in 2019'", "2019")),
    ("I've been practicing for the past seven years",
     ("'for the past seven years'", "2016")),
])
def test_derive_evidence_rules(text, expected):
    out = dict(_derive_evidence_lines(text, TS))
    assert out[expected[0]] == expected[1]


def test_derive_last_weekday_walk():
    # 2023-05-08 is a Monday; "last Saturday" must walk back to
    # 2023-05-06 (the Saturday strictly before the anchor)
    out = dict(_derive_evidence_lines("I ran a race last Saturday", TS))
    assert out["'last Saturday'"] == "2023-05-06"


def test_derive_n_unit_ago():
    out = dict(_derive_evidence_lines("I moved two months ago", TS))
    assert out["'two months ago'"] == "2023-03-08"


def test_derive_no_ts_only_literal_years():
    out = _derive_evidence_lines("I started in 2016", None)
    assert ("'in 2016'", "2016") in out
    assert len(out) == 1


# ------------------------- absolute anchors -------------------------------

@pytest.mark.parametrize("question,n_windows", [
    ("Who did Maria have dinner with on May 3, 2023?", 1),
    ("Which places in Canada was Evan visiting in July 2023?", 1),
    ("Which country were Jolene and her mother visiting in 2010?", 1),
    ("Where was John between August 11 and August 15 2023?", 2),
    ("Which country was Jolene located in during the last week of August 2023?", 2),
])
def test_absolute_anchors_found(question, n_windows):
    anchors = _parse_absolute_anchors(question)
    assert len(anchors) >= n_windows


def test_absolute_anchors_absent():
    assert _parse_absolute_anchors("What is Nate's favorite game?") == []
    assert _bare_month_anchor("What is Nate's favorite game?") is None


def test_bare_month_anchor():
    assert _bare_month_anchor("Which outdoor spot did Joanna visit in May?") == "05"
    # "in May 2023" is NOT a bare-month anchor (it has a year)
    assert _bare_month_anchor("Which spot did she visit in May 2023?") is None


def test_anchor_windows_are_iso_pairs():
    for start, end, _ in _parse_absolute_anchors(
            "Who did Maria have dinner with on May 3, 2023?"):
        assert start <= end
        assert start.startswith("2023-05-0")
        assert end.startswith("2023-05-0")


# ------------------------- when judge --------------------------------------

def _ev_section(derived, observed="2023-05-08", text="the evidence chunk"):
    return (f"## TEMPORAL EVIDENCE (relative-time resolution)\n"
            f"- [derived: {derived} | observed: {observed}] {text}")


def test_judge_when_year():
    cb = _ev_section("'last year' -> 2022")
    assert _judge_when(cb, "2022")
    assert _judge_when(cb, "Since 2022")
    assert not _judge_when(cb, "2019")


def test_judge_when_full_date_format_independent():
    cb = _ev_section("'yesterday' -> 2023-05-07")
    assert _judge_when(cb, "7 May 2023")
    assert _judge_when(cb, "May 7, 2023")
    assert _judge_when(cb, "2023-05-07")
    # the observed session date is accepted by design (gold answers
    # that ARE the session date of the answer chunk)
    assert _judge_when(cb, "8 May 2023")
    # a date that is neither derived nor observed must fail
    assert not _judge_when(cb, "9 May 2023")


def test_judge_when_weekday_relative():
    # "the sunday before 25 May 2023" = 2023-05-21
    cb = _ev_section("'last Saturday' -> 2023-05-20")
    # 2023-05-20 is a Saturday, not the Sunday before the 25th
    assert not _judge_when(cb, "The sunday before 25 May 2023")
    cb2 = _ev_section("'yesterday' -> 2023-05-21")
    assert _judge_when(cb2, "The sunday before 25 May 2023")


def test_judge_when_requires_section():
    assert not _judge_when("plain context with 2022 in it", "2022")


# ------------------------- number-word judge ------------------------------

def test_numberword_normalize():
    assert _numberword_normalize("six months") == "6 months"
    assert _numberword_normalize("a dozen eggs") == "a 12 eggs"


def test_judge_numberword_nugget():
    cb = "## VERBATIM CHUNKS\n- Joanna: It took me 4 months to finish it."
    assert _judge_numberword_nugget(cb, "four months")
    assert not _judge_numberword_nugget(cb, "three months")
    # no number word in gold -> judge declines (passthrough territory)
    assert not _judge_numberword_nugget(cb, "4 months")


# ------------------------- adversarial rubric ------------------------------

def test_adversarial_abstain():
    # trap never surfaced -> correct (no false endorsement)
    ok, note = adversarial_correct(
        "- Melanie: I love pottery.", "What did Caroline realize?",
        "self-care is important", ["Caroline", "Melanie"])
    assert ok and note == "adversarial_abstain"


def test_adversarial_misattribution_visible():
    # trap surfaced but attributed to the OTHER speaker -> the
    # misattribution is visible in-band -> correct
    ok, note = adversarial_correct(
        "- Melanie: I'm starting to realize that self-care is important.",
        "What did Caroline realize?", "self-care is important",
        ["Caroline", "Melanie"])
    assert ok and note == "adversarial_misattribution_visible"


def test_adversarial_surfaced_on_asked_speaker():
    ok, note = adversarial_correct(
        "- Caroline: I realize that self-care is important.",
        "What did Caroline realize?", "self-care is important",
        ["Caroline", "Melanie"])
    assert not ok and note == "adversarial_surfaced"


# ------------------------- top-5 budget -------------------------------------

def test_top5_prefers_verbatim_section():
    cb = ("[Retrieved evidence — chunk-recall path]\n"
          "- chunkrecall line 1\n- chunkrecall line 2\n"
          "## VERBATIM CHUNKS (BM25 + dense hybrid)\n"
          "- [score=0.9] verbatim line 1\n"
          "- [score=0.8] verbatim line 2\n"
          "- [score=0.7] verbatim line 3\n"
          "- [score=0.6] verbatim line 4\n"
          "- [score=0.5] verbatim line 5\n"
          "- [score=0.4] verbatim line 6\n")
    block = top5_block_from_cb(cb)
    assert "verbatim line 1" in block
    assert "verbatim line 5" in block
    assert "verbatim line 6" not in block
    assert "chunkrecall" not in block


def test_top5_falls_back_to_recall_lines():
    cb = "[Retrieved evidence — chunk-recall path]\n- only line\n"
    block = top5_block_from_cb(cb)
    assert "only line" in block


def test_top5_empty():
    assert "(no memories retrieved)" in top5_block_from_cb("")


# ------------------------- dates + flatten ---------------------------------

def test_parse_locomo_date():
    dt = parse_locomo_date("8:56 pm on 20 July, 2023")
    assert (dt.year, dt.month, dt.day) == (2023, 7, 20)
    assert parse_locomo_date("2023-05-08 10:00") is not None
    assert parse_locomo_date("not a date") is None
    assert parse_locomo_date("") is None


def test_flatten_locomo_conversation():
    conv = {
        "speaker_a": "Alice", "speaker_b": "Bob",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [
            {"speaker": "Alice", "dia_id": "D1:1",
             "text": "I work at Google.",
             "blip_caption": "a photo of a dog"},
            {"speaker": "Bob", "dia_id": "D1:2", "text": "Nice!"},
        ],
        "session_2_date_time": "2:00 pm on 9 May, 2023",
        "session_2": [
            {"speaker": "Alice", "dia_id": "D2:1",
             "text": "I left Google."},
        ],
    }
    msgs, meta = flatten_locomo_conversation(conv)
    assert len(msgs) == 3
    assert msgs[0]["content"].startswith("Alice: I work at Google.")
    assert "(image: a photo of a dog)" in msgs[0]["content"]
    assert msgs[0]["timestamp"] == datetime(2023, 5, 8,
                                             tzinfo=timezone.utc)
    assert msgs[2]["timestamp"] == datetime(2023, 5, 9,
                                            tzinfo=timezone.utc)
    assert len(meta) == 2


# ------------------------- judge chain --------------------------------------

def test_locomo_judge_falls_back_to_when():
    lq = None
    mem = None

    class _FakeQ:
        question = "When did Melanie paint a sunrise?"

    # det_judge would fail; the when fallback fires on the evidence
    # section. We can't easily call det_judge without a Memory, so
    # test the chain shape through a stub.
    from scripts.locomo_canonical import _WHEN_Q_RE
    assert _WHEN_Q_RE.search("When did Melanie paint a sunrise?")
    assert _WHEN_Q_RE.search("How long has she been practicing art?")
    assert _WHEN_Q_RE.search("What year did John start surfing?")
    assert not _WHEN_Q_RE.search("What is Nate's favorite game?")


# ------------------------- end-to-end smoke --------------------------------

def test_run_locomo_conversation_smoke(tmp_path):
    """One tiny synthetic conversation through the production path."""
    c = {
        "sample_id": "conv-test",
        "conversation": {
            "speaker_a": "Alice", "speaker_b": "Bob",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Alice", "dia_id": "D1:1",
                 "text": "I work at Google as a senior engineer."},
                {"speaker": "Bob", "dia_id": "D1:2",
                 "text": "That's great news!"},
                {"speaker": "Alice", "dia_id": "D1:3",
                 "text": "I painted a lake sunrise last year!"},
            ],
        },
        "qa": [
            {"question": "Where does Alice work?",
             "answer": "Google", "evidence": ["D1:1"], "category": 4},
            {"question": "When did Alice paint a sunrise?",
             "answer": "2022", "evidence": ["D1:3"], "category": 2},
            {"question": "What did Bob realize?",
             "adversarial_answer": "That's great news",
             "evidence": ["D1:2"], "category": 5},
        ],
    }
    res = run_locomo_conversation(c, 0, db_dir=str(tmp_path),
                                  max_seconds_per_conv=120.0)
    assert res["conversation_id"] == "conv-test"
    assert res["n_questions_scored"] == 3
    by_q = {r["question"]: r for r in res["results"]}
    assert by_q["Where does Alice work?"]["det_correct"] is True
    # relative-time derivation: "last year" in a 2023 session -> 2022
    assert by_q["When did Alice paint a sunrise?"]["det_correct"] is True
    assert by_q["When did Alice paint a sunrise?"]["evidence_chunks_added"] >= 1
    # adversarial: the trap text IS surfaced on the asked (Bob) line
    adv = by_q["What did Bob realize?"]
    assert adv["adversarial_correct"] is False
    assert adv["adversarial_note"] == "adversarial_surfaced"


def test_summarize_adversarial_uses_rubric():
    convs = [{
        "conversation_id": "conv-x", "conv_idx": 0,
        "speakers": ["A", "B"], "n_sessions": 1, "n_turns_ingested": 1,
        "n_questions": 2, "n_questions_scored": 2, "ingest_s": 1,
        "consolidate_s": 0, "total_s": 1,
        "results": [
            {"qid": "q1", "category": "single_hop", "gold": "X",
             "det_correct": True, "top5_correct": True,
             "adversarial_correct": None, "adversarial_note": "",
             "judge_strategy": "nugget", "retrieve_s": 0.01,
             "temporal_chunks_added": 0, "agg_chunks_added": 0,
             "age_chunks_added": 0, "evidence_chunks_added": 0,
             "absolute_window_chunks_added": 0, "speaker_chunks_added": 0,
             "context_block_preview": ""},
            {"qid": "q2", "category": "adversarial", "gold": "",
             "det_correct": False, "top5_correct": False,
             "adversarial_correct": True, "adversarial_note": "adversarial_abstain",
             "judge_strategy": "adversarial_rubric", "retrieve_s": 0.01,
             "temporal_chunks_added": 0, "agg_chunks_added": 0,
             "age_chunks_added": 0, "evidence_chunks_added": 0,
             "absolute_window_chunks_added": 0, "speaker_chunks_added": 0,
             "context_block_preview": ""},
        ],
    }]
    s = summarize(convs)
    # adversarial must be reported via the rubric, not a misleading det 0.0
    assert "adversarial (rubric)" in s["by_category"]
    assert s["by_category"]["adversarial (rubric)"] == 1.0
    assert s["by_category"]["single_hop"] == 1.0
    assert s["comparable_subset"]["counts"] == [1, 1]
