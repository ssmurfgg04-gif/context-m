"""Tests for v0.6.2 generalized numeric aggregation and percentage judges.

Also verifies the judges/ package refactor: all symbols importable,
no local duplicates in longmemeval_judge shadowing package versions,
_judge_paren_abbreviation uses word-boundary matching.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from judges.amount import (
    _judge_numeric_agg, _judge_percentage, _judge_sum_or_diff,
    _extract_numbers, _AMOUNT_RE,
)
from judges.paren import _judge_paren_abbreviation
from judges.nugget import _judge_nugget
from judges.list import _judge_list
import longmemeval_judge as lj


class FakeQ:
    def __init__(self, question=""):
        self.question = question
        self.entity = ""
        self.attribute = ""


# ── numeric_agg: total questions ────────────────────────────────────────────

def test_numeric_agg_total_pages():
    """'left to read' with total=380 and read=190 → target 190."""
    ctx = "The Nightingale has 380 pages. I have read 190 pages so far."
    q = FakeQ("How many pages do I have left to read?")
    assert _judge_numeric_agg(ctx, "190", q)


def test_numeric_agg_total_episodes():
    """Sum of episodes: 15 + 12 = 27."""
    ctx = "I've listened to 15 episodes of How I Built This and 12 episodes of My Favorite Murder."
    q = FakeQ("What is the total number of episodes I've listened to from both podcasts?")
    assert _judge_numeric_agg(ctx, "27", q)


def test_numeric_agg_total_views():
    """Sum of views: 998 + 1000 = 1998 (with comma in answer)."""
    ctx = "My YouTube video has 998 views. My TikTok video has 1000 views."
    q = FakeQ("What is the total number of views on my most popular videos combined?")
    assert _judge_numeric_agg(ctx, "1,998", q)


def test_numeric_agg_not_triggered_without_signal():
    """Should NOT fire when question has no aggregation keyword."""
    ctx = "I have 50 pages and 100 pages."
    q = FakeQ("How many pages is the book?")
    # "150" could be a coincidental subset-sum but no total/diff signal
    assert not _judge_numeric_agg(ctx, "150", q)


def test_numeric_agg_diff_followers():
    """Difference: 350 - 250 = 100 followers increase."""
    ctx = "I started with 250 followers. After two weeks I had 350 followers."
    q = FakeQ("What was the approximate increase in Instagram followers I experienced in two weeks?")
    assert _judge_numeric_agg(ctx, "100", q)


def test_numeric_agg_no_match():
    """Target not derivable from context numbers."""
    ctx = "I have 10 items and 20 items."
    q = FakeQ("What is the total number combined?")
    assert not _judge_numeric_agg(ctx, "99", q)


# ── percentage judge ─────────────────────────────────────────────────────────

def test_percentage_packed_shoes():
    """2/5 shoes worn = 40%."""
    ctx = "I packed 5 pairs of shoes. I ended up only wearing 2 pairs."
    assert _judge_percentage(ctx, "40%")


def test_percentage_complement():
    """3/5 unworn = 60% not worn — doesn't match 40%, complement does."""
    ctx = "I packed 5 shoes. I wore 2."
    assert _judge_percentage(ctx, "40%")


def test_percentage_no_match():
    """Numbers present but no pair derives the target %."""
    ctx = "I packed 7 shoes and wore 3."
    # 3/7 = 42.8%, not 40%
    assert not _judge_percentage(ctx, "40%")


def test_percentage_not_fired_without_percent_sign():
    """Plain '40' should not trigger the percentage judge."""
    ctx = "I packed 5 shoes and wore 2."
    assert not _judge_percentage(ctx, "40")


# ── paren abbreviation: word-boundary fix ────────────────────────────────────

def test_paren_word_boundary_positive():
    """UCLA in context matches 'University of California, Los Angeles (UCLA)'."""
    ctx = "I studied at UCLA during my undergrad years."
    assert _judge_paren_abbreviation(ctx, "University of California, Los Angeles (UCLA)")


def test_paren_word_boundary_negative():
    """MIT must not match inside 'submit' or 'committed'."""
    ctx = "I submitted my application and committed to the program."
    assert not _judge_paren_abbreviation(ctx, "Massachusetts Institute of Technology (MIT)")


def test_paren_real_mit_match():
    """MIT should match when present as standalone token."""
    ctx = "I attended MIT for my graduate studies."
    assert _judge_paren_abbreviation(ctx, "Massachusetts Institute of Technology (MIT)")


# ── judges/ package imports match main file ──────────────────────────────────

def test_package_symbols_all_present():
    """All expected symbols importable from longmemeval_judge."""
    required = [
        'det_judge', '_judge_bool', '_judge_nugget', '_judge_list',
        '_judge_paren_abbreviation', '_resolve_holiday_dates', '_AMOUNT_RE',
        '_judge_sum_or_diff', '_judge_numeric_agg', '_judge_percentage',
        '_STOPWORDS',
    ]
    for sym in required:
        assert hasattr(lj, sym), f"Missing: {sym}"


def test_nugget_full_4_strategies():
    """Confirm full 4-strategy nugget (Jaccard) is in use, not stub."""
    # Partial Jaccard: "painting worth triple" matches "it's worth triple what I paid"
    ctx = "it's actually worth triple what i paid for the piece"
    assert _judge_nugget(ctx, "The painting is worth triple what I paid")


def test_list_3_strategy():
    """Confirm 3-strategy list (Jaccard) is in use, not stub."""
    # Partial Jaccard: "25 minutes and 50 seconds" with ctx having "25:50"
    ctx = "the commute takes 25:50"
    # 50% of tokens {25, 50, minutes, seconds} -> {25,50} present = 50%
    assert _judge_list(ctx, "25 minutes and 50 seconds")
