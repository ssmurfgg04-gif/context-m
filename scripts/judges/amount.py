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
        r"\bhow\s+many\s+.*\b(?:total|altogether|combined)\b|"
        r"\b(?:page|word)\s+count\s+(?:of|for)\b|\bof\s+the\s+two\s+\w+\b",
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


# -------------------------- v0.6.5.1 derivation judges --------------------

_KINSHIP_MAP = {
    "parents": ["mom", "mother", "dad", "father", "parents"],
    "parent": ["mom", "mother", "dad", "father", "parent"],
    "grandparents": ["grandma", "grandmother", "grandpa",
                     "grandfather", "nana", "papa", "grandparents"],
    "grandparent": ["grandma", "grandmother", "grandpa",
                    "grandfather", "grandparent"],
    "grandma": ["grandma", "grandmother"],
    "grandpa": ["grandpa", "grandfather"],
    "mom": ["mom", "mother"],
    "dad": ["dad", "father"],
    "siblings": ["brother", "sister", "sibling"],
    "children": ["son", "daughter", "child"],
}


def _judge_average(context_block: str, answer: str, q: "object",
                   max_k: int = 8) -> bool:
    """AVERAGE judge: "average age of me, my parents, and my
    grandparents" → 59.6 = (32+55+58+75+78)/5.

    Count-tracking subset-sum: for each k in 2..max_k, does some
    k-element subset of context numbers sum to target*k? Bitset DP
    where each reachable sum carries a bitmask of achievable subset
    sizes. μ=0, deterministic, bounded by sum = target*k.
    """
    target = _parse_amount(answer)
    if target is None or target <= 0:
        return False
    # work in TENTHS so any 1-decimal target (59.6) stays exact
    t10 = int(round(target * 10))
    if abs(target * 10 - t10) > 1e-9 or t10 <= 0:
        return False  # 2+ decimal targets are out of scope
    cb = context_block or ""
    amounts = _extract_numbers(cb, include_dollar=False,
                               include_plain=True)
    if len(amounts) < 2:
        return False
    bound = t10 * max_k
    if bound > 200_000:  # guard: DP size stays microseconds
        return False
    # amounts scaled to tenths, deduped, bounded by target*max_k
    vals = sorted({int(round(a)) * 10 for a in amounts
                   if 0 < int(round(a)) * 10 <= bound})
    if len(vals) < 2:
        return False
    # reach[sum] = bitmask of subset sizes achieving that sum
    reach: dict[int, int] = {}
    for a in vals:
        new_reach = dict(reach)
        new_reach[a] = new_reach.get(a, 0) | 2  # bit 1 = size 1
        for s, mask in reach.items():
            ns = s + a
            if ns <= bound:
                new_reach[ns] = new_reach.get(ns, 0) | (mask << 1)
        reach = new_reach
        # early exit: any k in range already satisfied
        for k in range(2, max_k + 1):
            if (reach.get(t10 * k, 0) >> k) & 1:
                return True
    for k in range(2, max_k + 1):
        if (reach.get(t10 * k, 0) >> k) & 1:
            return True
    return False


_WAIT_RE = re.compile(
    r"\b(?:next|in\s+one|in\s+a)\s+(year|month|week)s?\b|"
    r"\bin\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(year|month|week)s?\b", re.I)


def _judge_will_be(context_block: str, answer: str, q: "object") -> bool:
    """FUTURE-AGE judge: "How many years will I be when my friend
    Rachel gets married?" → 33 = 32 ("I'm 32") + 1 ("next year").

    Parses the wait from the QUESTION's context match, verifies
    (target − wait) exists among context numbers. μ=0.
    """
    target = _parse_amount(answer)
    if target is None or target <= 0 or target > 120:
        return False
    cb = context_block or ""
    m = _WAIT_RE.search(cb)
    if not m:
        return False
    # alternation groups: alt1 → (unit); alt2 → (count, unit)
    gs = m.groups()
    unit = (gs[0] or gs[2] or "").lower()
    ntok = gs[1]
    if ntok is None:
        wait = 1  # "next year" / "in one year" / "in a year"
    else:
        wait = int(ntok) if ntok.isdigit() else _NUM_WORDS.get(
            ntok.lower())
    if not wait or wait > 60:
        return False
    # sanity: the wait unit must be years for an age question
    if unit and "year" not in unit:
        return False
    want = target - wait
    if want <= 0:
        return False
    amounts = _extract_numbers(cb, include_dollar=False,
                               include_plain=True)
    return want in [int(a) for a in amounts if a == int(a)]


_CLOCK_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?\b")
_OFFSET_RE = re.compile(
    r"\b(\d{1,3})\s*minutes?\s+(earlier|later)\b", re.I)


def _judge_clock_arithmetic(context_block: str, answer: str,
                            q: "object") -> bool:
    """CLOCK-ARITHMETIC judge: "What time do I wake up on Tuesdays and
    Thursdays?" → 6:45 AM = 7:00 AM − 15 minutes ("waking up 15
    minutes earlier").

    Fires only for "what time" questions. Parses clock times and
    minute offsets from the context, tries t ± offset for every
    parsed time, formats H:MM AM/PM, and compares to the answer.
    μ=0, deterministic.
    """
    a = (answer or "").strip().upper().replace(".", "").replace(" ", "")
    m_ans = re.match(r"^(\d{1,2}):(\d{2})(AM|PM|A|P)?$", a)
    if not m_ans:
        return False
    qtext = (getattr(q, "question", None) or "").lower()
    if "what time" not in qtext:
        return False
    cb = context_block or ""
    offsets = [(int(n), d.lower())
               for n, d in _OFFSET_RE.findall(cb)]
    if not offsets:
        return False
    h_ans, m_ans_ = int(m_ans.group(1)), int(m_ans.group(2))
    mer_ans = m_ans.group(3) or ""
    for h, mi, mer in _CLOCK_RE.findall(cb):
        try:
            h, mi = int(h), int(mi)
        except ValueError:
            continue
        if h > 23 or mi > 59:
            continue
        for n, d in offsets:
            delta = int(n) * (1 if d == "later" else -1)
            total = h * 60 + mi + delta
            total %= 24 * 60
            nh, nmi = divmod(total, 60)
            if (nh, nmi) != (h_ans, m_ans_):
                continue
            mer_s = (mer or "").upper()
            # accept when meridiem info is absent on either side, or
            # both agree on the half-day (A/P)
            if not mer_ans or not mer_s or mer_s[0] == mer_ans[0]:
                return True
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
