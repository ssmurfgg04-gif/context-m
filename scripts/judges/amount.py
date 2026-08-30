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
