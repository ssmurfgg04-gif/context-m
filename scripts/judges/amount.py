"""Amount parsing and subset-sum utilities for aggregation judges."""
from __future__ import annotations

import bisect
import re
from typing import Protocol

# v0.6.2: generalized to match both $-amounts and plain numbers
# v0.6.4: dollar-only regex split out of the alternation — the old
# combined form made group(2) capture EVERY number, so
# _extract_numbers(include_dollar=True, include_plain=True) counted
# each plain number twice (once per pass).
_DOLLAR_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_AMOUNT_RE = re.compile(r"(?:\$\s*([\d,]+(?:\.\d+)?)|\b([\d,]+(?:\.\d+)?)\b)")

# v0.6.5: number WORDS — canonical answers use them ("Two months",
# "three doctors") and haystack text uses them ("three months now",
# "a month ago"). Bounded map, no NLP.
_NUM_WORDS = {
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
    "eleven": 11.0, "twelve": 12.0, "thirteen": 13.0, "fifteen": 15.0,
    "twenty": 20.0, "thirty": 30.0, "forty": 40.0, "fifty": 50.0,
    "couple": 2.0, "few": 3.0, "dozen": 12.0, "half": 0.5,
}
_NUM_WORD_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fifteen|twenty|thirty|forty|fifty|couple|dozen)\b",
    re.I)


def _parse_amount(s: str) -> float | None:
    """Parse '$5,850', '5,850', '190 pages' → float.

    v0.6.5: also parses leading number words — "Two months" → 2.0,
    "three" → 3.0 — so duration/count answers can enter the
    aggregation judges at all.
    """
    m = _AMOUNT_RE.search(s)
    if not m:
        w = _NUM_WORD_RE.search(s or "")
        if w:
            return _NUM_WORDS[w.group(1).lower()]
        return None
    num = m.group(1) or m.group(2)
    if not num:
        return None
    try:
        return float(num.replace(",", ""))
    except ValueError:
        return None


def _subset_sum_matches(amounts: list[float], target: float,
                         tol: float = 0.01) -> bool:
    """Does any subset of >=2 amounts sum to target?

    v0.6.5: bitset dynamic programming bounded by the TARGET, not by
    the amount list. The v0.6.4 brute force enumerated 2^n subsets and
    truncated to the FIRST 20 amounts when the context was
    number-dense (a 120k-char aggregation context carries ~687
    numbers) — silently dropping the actual summands (d6062bb9:
    1,456 at index 63 and 542 at index 88 never made the cut, so
    "1,456 + 542 = 1,998" was judged underivable).

    The DP tracks reachable sums as bits of a Python int (one bit per
    integer sum, capped at target), with a separate bitset for sums
    reachable using >=2 elements. Complexity: O(unique_amounts x
    target/64) word ops — microseconds for canonical targets.
    Deterministic, μ=0.
    """
    t = int(round(target))
    if t <= 0:
        return False
    # guard against pathological targets (not in LongMemEval, but the
    # judge must never hang): fall back to a bounded brute force
    if t > 1_000_000:
        amounts = [a for a in amounts if a > 0][:20]
        n = len(amounts)
        if n < 2:
            return False
        for i in range(n):
            for j in range(i + 1, n):
                if abs(amounts[i] + amounts[j] - target) <= tol:
                    return True
        return False
    # dedupe integral amounts bounded by target
    ints = sorted({int(round(a)) for a in amounts
                   if 0 < a <= t})
    if len(ints) < 2:
        return False
    mask = (1 << (t + 1)) - 1
    reach = 0        # sums reachable with >=1 element
    reach_multi = 0  # sums reachable with >=2 elements
    for a in ints:
        if a > t:
            continue
        new_multi = (reach_multi | (reach << a)) & mask
        reach = (reach | (reach << a) | (1 << a)) & mask
        reach_multi = new_multi
        if (reach_multi >> t) & 1:
            return True
    return False


def _pair_difference_matches(amounts: list[float], target: float,
                              tol: float = 0.01) -> bool:
    """Does any |a - b| == target for two distinct amounts?"""
    n = len(amounts)
    if n < 2:
        return False
    seen: set[float] = set()
    for a in amounts:
        for b in seen:
            if abs(abs(a - b) - target) <= tol:
                return True
        seen.add(a)
    return False


# v0.6.2: generalized numeric extraction and aggregation judges
# v0.6.4: decimals are INSIDE the capture group ("2.5" used to be
# captured as "2"), and the dollar branch uses the $-anchored regex
# so plain numbers are never double-extracted.
_PLAIN_NUM_RE = re.compile(r"(?<![\d.\w])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?!\d)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _extract_numbers(text: str, include_dollar: bool = True,
                      include_plain: bool = True,
                      include_words: bool = False) -> list[float]:
    """Extract numeric values: dollar amounts and/or plain numbers.

    Masks percentages and dollar tokens before plain-number extraction
    to prevent double-counting.

    v0.6.5: ``include_words=True`` ALSO extracts number words ("three",
    "couple", ...). Off by default — words like "one" appear
    everywhere — and only enabled by the aggregation judges when the
    ANSWER itself is a number word ("Two months"), which keeps the
    blast radius tiny.
    """
    vals: list[float] = []
    t = text or ""
    if include_dollar:
        # $-prefixed amounts only (group 1) — the combined _AMOUNT_RE
        # alternation also captured plain numbers here, which were
        # then extracted AGAIN by the plain pass below.
        for m in _DOLLAR_RE.finditer(t):
            try:
                v = float(m.group(1).replace(",", ""))
                if v > 0:
                    vals.append(v)
            except ValueError:
                continue
    if include_plain:
        # Mask only the $-amount tokens (not all plain numbers) so their
        # digits aren't double-counted. Percentage tokens are also masked
        # since percentages are handled by _judge_percentage separately.
        dollar_re = re.compile(r"\$\s*[\d,]+(?:\.\d+)?")
        masked = _PERCENT_RE.sub(" ", t)
        masked = dollar_re.sub(" ", masked)
        for m in _PLAIN_NUM_RE.finditer(masked):
            try:
                v = float(m.group(1).replace(",", ""))
                if v > 0:
                    vals.append(v)
            except ValueError:
                continue
    if include_words:
        for m in _NUM_WORD_RE.finditer(t):
            v = _NUM_WORDS[m.group(1).lower()]
            if v > 0:
                vals.append(v)
        # "a/an <duration unit>" = 1 ("a month ago", "an hour ago").
        # Only fires in the word-answer path (include_words=True), so
        # the article-as-one reading never leaks into digit-answer
        # judging.
        for m in re.finditer(
                r"\b(?:a|an)\s+(?:day|week|month|year)s?\b",
                t, re.I):
            vals.append(1.0)
    return vals


def _judge_numeric_agg(context_block: str, answer: str, q: "object") -> bool:
    """Generalized SUM_OR_DIFF for plain-number answers (pages, episodes, etc.).

    Same subset-sum/pair-difference derivability as _judge_sum_or_diff
    but sources numbers from plain integers in addition to dollar amounts.
    Only fires when the question phrasing signals aggregation (total/diff)
    — plain numbers are too common in context to fire permissively.

    v0.6.5: adds two difference signals the canonical 500 exposed:
      * "how old was I when ..." (age at event = age now − years since)
      * "how long had I been ..." (duration = total − time-ago)
    and number-word answers ("Two months") also scan context words.
    """
    target = _parse_amount(answer)
    if target is None:
        return False

    cb = context_block or ""
    is_word_answer = bool(_NUM_WORD_RE.search(answer or "")) and \
        not _AMOUNT_RE.search(answer or "")
    amounts = _extract_numbers(cb, include_dollar=True, include_plain=True,
                               include_words=is_word_answer)
    if len(amounts) < 2:
        return False

    qtext = (getattr(q, "question", None) or "").lower()
    is_diff = bool(re.search(
        r"\bhow\s+much\s+(?:more|less|higher|lower|greater|smaller)"
        r".*\bcompared\s+to\b|\bdifference\s+(?:\w+\s+){0,3}between\b"
        r"|\b(?:increase|decrease)"
        r"\b.*\b(?:experienced|was|did|had)\b|\bleft\s+to\s+(?:read|go|finish)\b"
        r"|\bhow\s+old\s+was\s+i\s+when\b|\bhow\s+long\s+had\s+i\s+been\b",
        qtext, re.I))
    is_total = bool(re.search(
        r"\btotal\b|\bin\s+total\b|\bsum\s+of\b|\baltogether\b|\bcombined\b|"
        r"\bhow\s+many\s+.*\b(?:total|altogether|combined)\b",
        qtext, re.I))

    if is_diff:
        if _pair_difference_matches(amounts, target):
            return True
        return _subset_sum_matches(amounts, target)
    if is_total:
        return _subset_sum_matches(amounts, target)
    # No permissive fallback — plain numbers appear everywhere in context.
    # Require explicit aggregation signal in the question.
    return False


def _judge_percentage(context_block: str, answer: str) -> bool:
    """Bounded percentage judge for 'what percentage of X' questions.

    Checks whether any pair of plain numbers (a, b) in context derives
    the target percentage as a/b*100 or (b-a)/b*100 within 1pp tolerance.
    """
    pm = _PERCENT_RE.search(answer or "")
    if not pm:
        return False
    try:
        target = float(pm.group(1))
    except ValueError:
        return False
    if not (0 < target < 100):
        return False

    cb = context_block or ""
    nums = _extract_numbers(cb, include_dollar=False, include_plain=True)
    seen: list[float] = []
    for n in nums:
        if n not in seen:
            seen.append(n)
    if len(seen) < 2:
        return False

    tol = 1.0
    for a in seen:
        for b in seen:
            if a == b or b == 0 or a > b:
                continue
            if abs((a / b) * 100.0 - target) <= tol:
                return True
            if abs(((b - a) / b) * 100.0 - target) <= tol:
                return True
    return False


def _judge_sum_or_diff(context_block: str, answer: str, q: "object") -> bool:
    """SUM/DIFF judge for dollar-amount aggregation questions.

    Fires when the expected answer is a dollar amount ($X) or plain number
    AND the question signals aggregation (total, difference, etc.).
    Extracts amounts from context and verifies derivability via
    subset-sum (for totals) or pair-difference (for differences).

    v0.6.5: "how much did I save on X" questions are DIFFERENCE
    semantics (original price − paid price = savings), and
    "difference in price between" allows 0-3 words between
    "difference" and "between". Word-number answers ("Two months")
    scan context number-words too.
    μ=0: pure regex + arithmetic.
    """
    target = _parse_amount(answer)
    if target is None:
        return False
    cb = context_block or ""
    is_word_answer = bool(_NUM_WORD_RE.search(answer or "")) and \
        not _AMOUNT_RE.search(answer or "")
    amounts = _extract_numbers(cb, include_dollar=True, include_plain=True,
                               include_words=is_word_answer)
    if len(amounts) < 2:
        return False

    import re as _re
    qtext = (getattr(q, "question", None) or "").lower()
    is_diff = bool(_re.search(
        r"\bhow\s+much\s+(?:more|less|higher|lower|greater|smaller)"
        r".*\bcompared\s+to\b|\bdifference\s+(?:\w+\s+){0,3}between\b"
        r"|\bhow\s+much\s+(?:did\s+)?i\s+save\b|\bhow\s+much\s+did\s+i\s+save\s+on\b"
        r"|\bhow\s+much\s+have\s+i\s+saved\b|\bsavings?\s+on\b",
        qtext, _re.I))
    is_total = bool(_re.search(
        r"\b(?:total|in\s+total|all\s+the\s+\w+\s+(?:money|spent|earned|"
        r"raised|saved)|sum\s+of\s+all|how\s+much\s+(?:money\s+)?(?:did|have)"
        r"\s+I\s+(?:spent|spend|earned|earn|raised|raise|saved|save)"
        r"|how\s+much\s+total\s+money|what\s+is\s+the\s+total\s+amount)",
        qtext, _re.I))
    if is_diff:
        if _pair_difference_matches(amounts, target):
            return True
        return _subset_sum_matches(amounts, target)
    if is_total:
        return _subset_sum_matches(amounts, target)
    return False
