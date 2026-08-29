"""Quick smoke tests for v0.5.5 SUM/DIFF + holiday-date judges."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts.longmemeval_judge import (
    det_judge, LongMemEvalQuestion,
    _parse_amount, _subset_sum_matches, _pair_difference_matches,
    _resolve_holiday_dates, _judge_sum_or_diff,
)


def test_parse_amount():
    assert _parse_amount("$185") == 185.0
    assert _parse_amount("$5,850") == 5850.0
    assert _parse_amount("$3,300.50") == 3300.50
    assert _parse_amount("not a number") is None


def test_subset_sum():
    assert _subset_sum_matches([25, 40, 120], 185.0)
    assert _subset_sum_matches([25, 40, 120], 65.0)
    assert _subset_sum_matches([25, 40, 120], 145.0)
    assert not _subset_sum_matches([25, 40], 185.0)
    assert not _subset_sum_matches([100], 200.0)
    # Empty / single
    assert not _subset_sum_matches([100], 100.0)  # singleton excluded
    # Meet-in-the-middle (>10 amounts)
    amts = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110]
    target = sum(amts[:3])  # 60
    assert _subset_sum_matches(amts, target)


def test_pair_difference():
    assert _pair_difference_matches([300, 30], 270.0)
    assert _pair_difference_matches([30, 300], 270.0)
    assert _pair_difference_matches([100, 200, 300], 100.0)
    assert not _pair_difference_matches([100, 200, 300], 999.0)


def test_holiday_dates():
    cb = "I volunteered at the Love is in the Air fundraising dinner back on Valentine's Day."
    assert _resolve_holiday_dates(cb, "February 14th")
    assert not _resolve_holiday_dates(cb, "March 17th")
    # Reverse direction: answer is holiday, chunk has date
    cb2 = "On February 14th I went to the gala."
    assert _resolve_holiday_dates(cb2, "Valentine's Day")  # reverse works too
    # Christmas
    cb3 = "We opened gifts on Christmas Day."
    assert _resolve_holiday_dates(cb3, "December 25th")


def test_judge_sum_or_diff():
    # Bike expenses: $25 + $40 + $120 = $185
    cb = ("I replaced the chain which cost me $25. I got new bike lights "
          "for $40. I bought a Bell Zephyr helmet for $120.")
    q = LongMemEvalQuestion(
        session_id=1, question="How much total money have I spent on "
        "bike-related expenses since the start of the year?",
        answer="$185", subtask="multi_session")
    assert _judge_sum_or_diff(cb, "$185", q), "bike total should match"

    # Hawaii vs Tokyo diff
    cb2 = ("I stayed in a hostel in Tokyo that cost around $30 per night. "
           "In Hawaii I paid $300 per night for the hotel.")
    q2 = LongMemEvalQuestion(
        session_id=1, question="How much more did I spend on accommodations "
        "per night in Hawaii compared to Tokyo?",
        answer="$270", subtask="multi_session")
    assert _judge_sum_or_diff(cb2, "$270", q2), "hawaii-tokyo diff should match"

    # Charity events raised total = $5,850
    cb3 = ("I raised $2,000 for the local animal shelter. The charity run "
           "raised $1,850. The gala raised $2,000 from sponsors.")
    q3 = LongMemEvalQuestion(
        session_id=1, question="How much money did I raise in total "
        "through all the charity events I participated in?",
        answer="$5,850", subtask="multi_session")
    assert _judge_sum_or_diff(cb3, "$5,850", q3), "charity total should match"


def test_det_judge_routes_sum():
    """det_judge should route aggregation answers through sum_or_diff."""
    cb = ("Bike chain $25. Bike lights $40. Helmet $120.")
    q = LongMemEvalQuestion(
        session_id=1, question="How much total money have I spent on "
        "bike-related expenses?", answer="$185",
        subtask="multi_session")
    # Use a dummy Memory (None) — sum_or_diff doesn't touch mem
    correct, strat = det_judge(cb, "$185", mem=None, q=q)
    assert correct, "should be correct"
    assert strat == "sum_or_diff", f"expected sum_or_diff, got {strat}"


def test_det_judge_holiday():
    cb = "I volunteered at the Love is in the Air dinner on Valentine's Day."
    q = LongMemEvalQuestion(
        session_id=1,
        question="When did I volunteer at the local animal shelter's "
                 "fundraising dinner?",
        answer="February 14th", subtask="single_session")
    correct, strat = det_judge(cb, "February 14th", mem=None, q=q)
    assert correct, "valentine should resolve"
    assert strat == "holiday_date", f"expected holiday_date, got {strat}"


def test_det_judge_paren_abbreviation():
    """LongMemEval expected answer expands UCLA to full name."""
    cb = "I completed my undergrad in CS from UCLA."
    q = LongMemEvalQuestion(
        session_id=1,
        question="Where did I complete my Bachelor's degree in "
                 "Computer Science?",
        answer="University of California, Los Angeles (UCLA)",
        subtask="single_session")
    correct, strat = det_judge(cb, "University of California, Los Angeles (UCLA)",
                                mem=None, q=q)
    assert correct, "UCLA paren-abbreviation should match"
    assert strat == "paren_abbreviation", f"expected paren_abbreviation, got {strat}"


if __name__ == "__main__":
    test_parse_amount()
    print("✓ test_parse_amount")
    test_subset_sum()
    print("✓ test_subset_sum")
    test_pair_difference()
    print("✓ test_pair_difference")
    test_holiday_dates()
    print("✓ test_holiday_dates")
    test_judge_sum_or_diff()
    print("✓ test_judge_sum_or_diff")
    test_det_judge_routes_sum()
    print("✓ test_det_judge_routes_sum")
    test_det_judge_holiday()
    print("✓ test_det_judge_holiday")
    test_det_judge_paren_abbreviation()
    print("✓ test_det_judge_paren_abbreviation")
    print("\nAll v0.5.5 judge smoke tests passed.")
