"""Amount parsing and subset-sum utilities for aggregation judges."""
from __future__ import annotations

import bisect
import re
from typing import Protocol

# v0.6.2: generalized to match both $-amounts and plain numbers
_AMOUNT_RE = re.compile(r"(?:\$\s*([\d,]+(?:\.\d+)?)|\b([\d,]+(?:\.\d+)?)\b)")


def _parse_amount(s: str) -> float | None:
    """Parse '$5,850', '5,850', '190 pages' → float."""
    m = _AMOUNT_RE.search(s)
    if not m:
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
    """Does any subset of >=2 amounts sum to target? Meet-in-the-middle."""
    amounts = [a for a in amounts if a > 0]
    n = len(amounts)
    if n < 2:
        return False
    if n > 20:
        amounts = amounts[:20]
        n = 20
    if n <= 10:
        for mask in range(3, (1 << n)):
            s = 0.0
            count = 0
            for i in range(n):
                if mask & (1 << i):
                    s += amounts[i]
                    count += 1
            if count >= 2 and abs(s - target) <= tol:
                return True
        return False
    half = n // 2
    left = amounts[:half]
    right = amounts[half:]

    def _enumerate(arr: list[float]) -> list[tuple[float, int]]:
        out: list[tuple[float, int]] = []
        for mask in range(0, 1 << len(arr)):
            s = 0.0
            cnt = 0
            for i in range(len(arr)):
                if mask & (1 << i):
                    s += arr[i]
                    cnt += 1
            out.append((s, cnt))
        return out

    left_sums = _enumerate(left)
    right_sums = _enumerate(right)
    right_arr = sorted(right_sums)
    right_vals = [r[0] for r in right_arr]

    for ls, lsize in left_sums:
        need = target - ls
        i = bisect.bisect_left(right_vals, need - tol)
        while i < len(right_vals) and right_vals[i] <= need + tol:
            if lsize + right_arr[i][1] >= 2:
                return True
            i += 1
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
_PLAIN_NUM_RE = re.compile(r"(?<![\d.\w])(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _extract_numbers(text: str, include_dollar: bool = True,
                      include_plain: bool = True) -> list[float]:
    """Extract numeric values: dollar amounts and/or plain numbers.

    Masks percentages and dollar tokens before plain-number extraction
    to prevent double-counting.
    """
    vals: list[float] = []
    t = text or ""
    if include_dollar:
        for m in _AMOUNT_RE.finditer(t):
            try:
                num = m.group(1) or m.group(2)
                if num:
                    v = float(num.replace(",", ""))
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
    return vals


def _judge_numeric_agg(context_block: str, answer: str, q: "object") -> bool:
    """Generalized SUM_OR_DIFF for plain-number answers (pages, episodes, etc.).

    Same subset-sum/pair-difference derivability as _judge_sum_or_diff
    but sources numbers from plain integers in addition to dollar amounts.
    Only fires when the question phrasing signals aggregation (total/diff)
    — plain numbers are too common in context to fire permissively.
    """
    target = _parse_amount(answer)
    if target is None:
        return False

    cb = context_block or ""
    amounts = _extract_numbers(cb, include_dollar=True, include_plain=True)
    if len(amounts) < 2:
        return False

    qtext = (getattr(q, "question", None) or "").lower()
    is_diff = bool(re.search(
        r"\bhow\s+much\s+(?:more|less|higher|lower|greater|smaller)"
        r".*\bcompared\s+to\b|\bdifference\s+between\b|\b(?:increase|decrease)"
        r"\b.*\b(?:experienced|was|did|had)\b|\bleft\s+to\s+(?:read|go|finish)\b",
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
    μ=0: pure regex + arithmetic.
    """
    target = _parse_amount(answer)
    if target is None:
        return False
    cb = context_block or ""
    amounts: list[float] = []
    for m in _AMOUNT_RE.finditer(cb):
        try:
            num = m.group(1) or m.group(2)
            if not num:
                continue
            v = float(num.replace(",", ""))
            if v > 0:
                amounts.append(v)
        except ValueError:
            continue
    if len(amounts) < 2:
        return False

    import re as _re
    qtext = (getattr(q, "question", None) or "").lower()
    is_diff = bool(_re.search(
        r"\bhow\s+much\s+(?:more|less|higher|lower|greater|smaller)"
        r".*\bcompared\s+to\b|\bdifference\s+between\b",
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
