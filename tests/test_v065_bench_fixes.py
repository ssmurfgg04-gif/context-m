"""v0.6.5 benchmark-pipeline fixes — regression tests.

Covers the fixes derived from the 21 real v0.6.4 canonical-500
failures (the aggregate's true score was 0.958, not the contaminated
0.944) plus the pipeline hygiene that made the contamination
possible:

  1. split_long_message      — assistant segmentation (no data loss)
  2. parse_session_date      — haystack date → UTC datetime
  3. parse_temporal_window   — "two weeks ago" / "last Saturday" /
                               "past month" anchor resolution
  4. _subset_sum_matches     — bitset DP (late-index summands)
  5. judge signal extensions — save-on / age-when / duration /
                               difference-in-price-between /
                               number-word answers
  6. markdown-escape normalization in det_judge
  7. aggregate contamination guard (verdict-flip refusal)

All μ=0: no LLM anywhere in these code paths.
"""
from __future__ import annotations

import json
import os
import sys
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from scripts.longmemeval_canonical import (
    split_long_message, parse_session_date, _flatten_haystack_rich,
)
from scripts.longmemeval_canonical_full import (
    parse_temporal_window, _is_aggregation_question,
    _AGGREGATION_Q_RE,
)
from scripts.longmemeval_judge import det_judge, LongMemEvalQuestion


# ----------------------- 1. assistant segmentation -----------------------

class TestSplitLongMessage:
    def test_short_message_untouched(self):
        assert split_long_message("hello world", 2000) == ["hello world"]

    def test_no_content_loss(self):
        text = ("This is sentence one. Sentence two follows! Does three "
                "matter? ") * 60
        segs = split_long_message(text, 2000)
        assert all(len(s) <= 2000 for s in segs)
        # every word survives (whitespace may normalize)
        words_src = set(text.split())
        words_dst = set(" ".join(segs).split())
        assert words_src <= words_dst

    def test_deterministic(self):
        text = ("A. " + "x" * 120 + ". B. " + "y" * 120 + ". ") * 15
        assert (split_long_message(text, 500)
                == split_long_message(text, 500))

    def test_veja_position_survives(self):
        # canonical 1de5cff2: 'Veja' at pos 903/1279 — the 800-cap
        # dropped it; segmentation must keep it.
        text = ("Brand filler sentence. " * 44) + "5. Veja - French brand."
        segs = split_long_message(text, 2000)
        assert any("Veja" in s for s in segs)

    def test_hard_split_on_giant_sentence(self):
        # no sentence boundaries at all — hard split must not hang
        text = "z" * 9000
        segs = split_long_message(text, 2000)
        assert sum(len(s) for s in segs) >= 9000 - 10


# ----------------------- 2. session date parsing -------------------------

class TestParseSessionDate:
    def test_canonical_format(self):
        from datetime import timezone
        d = parse_session_date("2023/05/20 (Sat) 14:29")
        assert d is not None
        assert (d.year, d.month, d.day, d.hour, d.minute) == \
            (2023, 5, 20, 14, 29)
        assert d.tzinfo == timezone.utc

    def test_garbage_returns_none(self):
        assert parse_session_date("") is None
        assert parse_session_date("not a date") is None
        assert parse_session_date(None) is None


# ----------------------- 3. temporal window parsing ----------------------

class TestParseTemporalWindow:
    def test_two_weeks_ago(self):
        from datetime import datetime, timezone, timedelta
        qd = datetime(2023, 5, 20, 14, 29, tzinfo=timezone.utc)
        w = parse_temporal_window(
            "I mentioned a sports event two weeks ago.", qd)
        assert w is not None
        start, end, label = w
        target = qd - timedelta(days=14)
        assert start <= target <= end
        assert "two weeks ago" in label

    def test_ten_days_ago_digit_form(self):
        from datetime import datetime, timezone, timedelta
        qd = datetime(2023, 5, 20, tzinfo=timezone.utc)
        w = parse_temporal_window("What did I buy 10 days ago?", qd)
        assert w is not None
        start, end, _ = w
        assert start <= qd - timedelta(days=10) <= end

    def test_last_saturday_strictly_before(self):
        # gpt4_d6585ce9: question asked ON Saturday 2023/04/22;
        # "last Saturday" = 2023/04/15 (7 days back), NOT the same day.
        from datetime import datetime, timezone
        qd = datetime(2023, 4, 22, 8, 1, tzinfo=timezone.utc)
        w = parse_temporal_window(
            "Who did I go with to the music event last Saturday?", qd)
        assert w is not None
        start, end, _ = w
        assert start.date() <= datetime(2023, 4, 15).date() \
            <= end.date()
        assert datetime(2023, 4, 22).date() not in (
            d for d in [start.date()])  # anchor day excluded

    def test_past_month_window(self):
        from datetime import datetime, timezone
        qd = datetime(2023, 5, 20, tzinfo=timezone.utc)
        w = parse_temporal_window(
            "How many parties have I attended in the past month?", qd)
        assert w is not None
        start, end, _ = w
        # widened window covers BOTH readings (this month / last month)
        assert (end - start).days >= 60

    def test_no_anchor_returns_none(self):
        from datetime import datetime, timezone
        qd = datetime(2023, 5, 20, tzinfo=timezone.utc)
        assert parse_temporal_window(
            "What is my favorite color?", qd) is None

    def test_missing_question_date_returns_none(self):
        assert parse_temporal_window("two weeks ago", None) is None


# ----------------------- 4. subset-sum bitset DP -------------------------

class TestSubsetSumDP:
    def _f(self, amounts, target):
        from judges import _subset_sum_matches
        return _subset_sum_matches(amounts, target)

    def test_basic_sum(self):
        assert self._f([200, 100], 300)

    def test_single_element_does_not_match(self):
        assert not self._f([300], 300)
        assert not self._f([5], 5)

    def test_not_derivable(self):
        assert not self._f([500, 200], 300)

    def test_late_index_summands_d6062bb9(self):
        # the v0.6.4 brute force truncated to the first 20 amounts;
        # 687-number contexts dropped the real summands at index 63/88
        amounts = [float(x) for x in range(1, 60)] + [1456.0, 542.0]
        assert self._f(amounts, 1998)

    def test_negative_sparse(self):
        assert not self._f([10.0, 20.0, 40.0, 80.0, 160.0, 320.0,
                            640.0, 1280.0], 1998)

    def test_fast_on_dense_context(self):
        import time
        amounts = [float(x % 900) for x in range(687)]
        t0 = time.time()
        self._f(amounts, 1500)
        assert time.time() - t0 < 1.0

    def test_pathological_target_fallback(self):
        # target > 1M exercises the bounded fallback (no hang)
        assert not self._f([2.0, 3.0], 5_000_000_000)


# ----------------------- 5. judge signal extensions ----------------------

class TestJudgeSignalExtensions:
    def _q(self, question):
        return LongMemEvalQuestion(
            session_id=0, question=question, answer="",
            subtask="multi_session", entity="", attribute="")

    def test_save_on_is_difference(self):
        from judges import _judge_sum_or_diff
        ctx = ("I got it for $200. The handbag originally cost $500 "
               "at the designer store.")
        q = self._q("How much did I save on the designer handbag?")
        assert _judge_sum_or_diff(ctx, "$300", q)

    def test_difference_in_price_between(self):
        from judges import _judge_sum_or_diff
        ctx = ("The luxury boots cost $800. The budget store had a "
               "similar pair for $50.")
        q = self._q("What is the difference in price between my luxury "
                    "boots and the similar pair found at the budget store?")
        assert _judge_sum_or_diff(ctx, "$750", q)

    def test_age_when_moved(self):
        from judges import _judge_numeric_agg
        ctx = ("I am a 32-year-old male. I have been living in the "
               "United States for the past 5 years on a work visa.")
        q = self._q("How old was I when I moved to the United States?")
        assert _judge_numeric_agg(ctx, "27", q)

    def test_duration_word_answer(self):
        from judges import _judge_numeric_agg
        ctx = ("I've been getting into bird watching for about three "
               "months now. I attended a workshop a month ago.")
        q = self._q("How long had I been bird watching when I attended "
                    "the bird watching workshop?")
        assert _judge_numeric_agg(ctx, "Two months", q)

    def test_number_word_parsing(self):
        from judges import _parse_amount
        assert _parse_amount("Two months") == 2.0
        assert _parse_amount("three") == 3.0
        assert _parse_amount("$5,850") == 5850.0

    def test_word_numbers_not_extracted_by_default(self):
        from judges import _extract_numbers
        # include_words defaults OFF — "one" appears everywhere
        assert 1.0 not in _extract_numbers("one of the one things")

    def test_aggregation_gate_covers_spend(self):
        # ef9cf60a: "How much did I spend on gifts for my sister?"
        assert _AGGREGATION_Q_RE.search(
            "How much did I spend on gifts for my sister?")
        assert _is_aggregation_question(
            "How much did I spend on gifts for my sister?")

    def test_aggregation_gate_covers_total_number(self):
        # d6062bb9: "What is the total number of views..."
        assert _is_aggregation_question(
            "What is the total number of views on my most popular "
            "videos on YouTube and TikTok?")

    def test_no_false_positive_on_plain_questions(self):
        assert not _is_aggregation_question("What is my favorite color?")
        assert not _is_aggregation_question(
            "Where did I complete my Bachelor's degree?")


# ----------------------- 6. markdown-escape normalization ----------------

class TestMarkdownEscapeNormalization:
    def _mem(self):
        from cortexm.config import Config
        from cortexm.api.memory import Memory
        return Memory(Config(db_path=":memory:"))

    def test_handle_with_escaped_underscores(self):
        # b759caee: assistant text escapes underscores in handles
        ctx = ("1. Jessica Poole (@jessica\\_poole\\_jewellery): "
               "Jessica is a UK-based jewelry designer.")
        q = LongMemEvalQuestion(
            session_id=0, answer="@jessica_poole_jewellery",
            question="Which designer's Instagram account?",
            subtask="single_session", entity="", attribute="")
        mem = self._mem()
        try:
            ok, strat = det_judge(ctx, "@jessica_poole_jewellery",
                                  mem, q)
            assert ok and strat == "nugget"
        finally:
            mem.close()

    def test_plain_context_unaffected(self):
        ctx = "I love pizza and sushi."
        q = LongMemEvalQuestion(
            session_id=0, answer="pizza", question="What do I love?",
            subtask="single_session", entity="", attribute="")
        mem = self._mem()
        try:
            ok, _ = det_judge(ctx, "pizza", mem, q)
            assert ok
        finally:
            mem.close()


# ----------------------- 7. aggregate contamination guard ----------------

class TestAggregateGuard:
    def _write(self, path, results):
        with open(path, "w") as f:
            json.dump({"results": results}, f)

    def test_verdict_flip_refused(self, tmp_path):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        self._write(a, [{"global_idx": 0, "qid": "q0", "subtask":
                         "single_session", "det_correct": True,
                         "judge_strategy": "nugget"}])
        self._write(b, [{"global_idx": 0, "qid": "q0", "subtask":
                         "single_session", "det_correct": False,
                         "judge_strategy": "nugget"}])
        out = tmp_path / "out.json"
        proc = subprocess.run(
            [sys.executable,
             os.path.join(SCRIPTS, "longmemeval_canonical_aggregate.py"),
             "--slices", str(a), str(b), "--out", str(out)],
            capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode == 2, proc.stderr
        assert "VERDICT FLIPS" in proc.stderr

    def test_clean_aggregate_passes_with_provenance(self, tmp_path):
        a = tmp_path / "a.json"
        self._write(a, [{"global_idx": 0, "qid": "q0", "subtask":
                         "single_session", "det_correct": True,
                         "judge_strategy": "nugget"},
                        {"global_idx": 1, "qid": "q1", "subtask":
                         "multi_session", "det_correct": False,
                         "judge_strategy": "sum_or_diff"}])
        out = tmp_path / "out.json"
        proc = subprocess.run(
            [sys.executable,
             os.path.join(SCRIPTS, "longmemeval_canonical_aggregate.py"),
             "--slices", str(a), "--out", str(out)],
            capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr
        d = json.load(open(out))
        assert d["summary"]["det_judge_accuracy"] == 0.5
        prov = d["summary"]["aggregate_provenance"]
        assert "git_sha" in prov
        assert prov["slice_files"]["a.json"] == 2


# ----------------------- 8. rich flatten ----------------------------------

class TestFlattenHaystackRich:
    def test_timestamps_and_segmentation(self):
        sessions = [
            [{"role": "user", "content": "I live in Munich."},
             {"role": "assistant",
              "content": "Great! " * 600}],   # ~3600 chars
        ]
        dates = ["2023/05/20 (Sat) 14:29"]
        msgs = _flatten_haystack_rich(sessions, haystack_dates=dates)
        assert all(m["timestamp"] is not None for m in msgs)
        assert msgs[0]["role"] == "user"
        # the long assistant message is SEGMENTED (not truncated):
        # 3600 chars → >= 2 segments, all <= 2000
        asst = [m for m in msgs if m["role"] == "assistant"]
        assert len(asst) >= 2
        assert all(len(m["content"]) <= 2000 for m in asst)
        joined = "".join(m["content"] for m in asst)
        assert "Great!" in joined  # no content loss

    def test_assistant_can_be_excluded(self):
        sessions = [[{"role": "assistant", "content": "hi"}]]
        msgs = _flatten_haystack_rich(sessions, include_assistant=False)
        assert msgs == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
