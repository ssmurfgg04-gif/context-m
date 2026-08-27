"""Deterministic date-expression resolution (μ=0 temporal parsing).

Extracts absolute and relative date expressions from text, resolved
against the conversation timestamp — the substrate for bi-temporal
valid times and BEAM temporal-reasoning abilities.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from context_m.util import month_name, month_number

MONTH_RE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"

ABS_PATTERNS = [
    # March 3, 2024 / March 3rd 2024 / March 3, 2024
    (re.compile(rf"\b(?:on\s+)?({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "day"),
    # 3 March 2024
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_RE})\.?,?\s+(\d{{4}})\b", re.I), "day_r"),
    # March 3 (no year — assume year of context ts, prefer past)
    (re.compile(rf"\b(?:on\s+)?({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{{4}})", re.I), "day_noyear"),
    # March 2024 (month granularity)
    (re.compile(rf"\bin\s+({MONTH_RE})\.?,?\s+(\d{{4}})\b", re.I), "month"),
    (re.compile(rf"\b({MONTH_RE})\s+(\d{{4}})\b"), "month"),
    # 2025-08-15 (ISO day)
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso_day"),
    # 2025-08 / 2025/08 / "2025 08" (numeric year-month; before bare year)
    (re.compile(r"\b(\d{4})[-/ ](\d{1,2})\b"), "ym_num"),
    (re.compile(r"\bin\s+(\d{4})\b"), "year"),
]

REL_PATTERNS = [
    (re.compile(r"\byesterday\b", re.I), "yesterday"),
    (re.compile(r"\b(today|this morning|right now)\b", re.I), "today"),
    (re.compile(r"\btomorrow\b", re.I), "tomorrow"),
    (re.compile(r"\blast\s+night\b", re.I), "yesterday"),
    (re.compile(rf"\blast\s+({MONTH_RE})\b", re.I), "last_month"),
    (re.compile(r"\blast\s+week\b", re.I), "last_week"),
    (re.compile(r"\blast\s+month\b", re.I), "last_month_rel"),
    (re.compile(r"\blast\s+year\b", re.I), "last_year"),
    (re.compile(r"\b(\d+|a|an|one|two|three|four|five|six|few|several|couple)\s+(day|week|month|year)s?\s+ago\b", re.I), "ago"),
    (re.compile(rf"\b(?:in|since)\s+({MONTH_RE})\b(?!\s*,?\s*\d{{4}})", re.I), "this_year_month"),
    (re.compile(r"\bsince\s+(\d{4})\b"), "year"),
]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LAST_WEEKDAY = re.compile(rf"\blast\s+({'|'.join(WEEKDAYS)})\b", re.I)


def _month_num(name: str) -> int:
    return month_number(name)


def _mk(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def resolve_relative(label: str, m: re.Match, ts: datetime) -> tuple[str, str] | None:
    """Return (iso_date, surface) for a relative expression."""
    g = m.group(0).lower()
    if label == "yesterday":
        d = ts - timedelta(days=1)
        return _mk(d.year, d.month, d.day), g
    if label == "today":
        return _mk(ts.year, ts.month, ts.day), g
    if label == "tomorrow":
        d = ts + timedelta(days=1)
        return _mk(d.year, d.month, d.day), g
    if label == "last_week":
        d = ts - timedelta(days=7)
        return _mk(d.year, d.month, d.day), g
    if label == "last_month_rel":
        d = ts.replace(day=1) - timedelta(days=1)
        return _mk(d.year, d.month, 1), g
    if label == "last_year":
        return _mk(ts.year - 1, 1, 1), g
    if label == "last_month":
        mm = _month_num(m.group(1))
        y = ts.year if mm <= ts.month else ts.year - 1
        return _mk(y, mm, 1), g
    if label == "ago":
        raw = m.group(1).lower()
        words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
                 "four": 4, "five": 5, "six": 6, "few": 3, "several": 4,
                 "couple": 2}
        n = words.get(raw, 1) if not raw.isdigit() else int(raw)
        unit = m.group(2).lower()
        if unit.startswith("day"):
            d = ts - timedelta(days=n)
        elif unit.startswith("week"):
            d = ts - timedelta(weeks=n)
        elif unit.startswith("month"):
            d = ts.replace(day=1)
            for _ in range(n):
                d = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
            return _mk(d.year, d.month, d.day), g
        else:
            return _mk(ts.year - n, ts.month, ts.day), g
        return _mk(d.year, d.month, d.day), g
    if label == "this_year_month":
        mm = _month_num(m.group(1))
        y = ts.year if mm <= ts.month else ts.year - 1
        return _mk(y, mm, 1), g
    if label == "year":
        return f"{m.group(1)}-01-01", g
    return None


def find_dates(text: str, ts: datetime) -> list[dict]:
    """All date expressions: [{span, iso, surface, granularity}]."""
    out: list[dict] = []
    taken: list[tuple[int, int]] = []

    def overlaps(s, e):
        return any(not (e <= a or s >= b) for a, b in taken)

    for rx, label in ABS_PATTERNS:
        for m in rx.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            try:
                if label == "day":
                    mo, d, y = _month_num(m.group(1)), int(m.group(2)), int(m.group(3))
                elif label == "day_r":
                    d, mo, y = int(m.group(1)), _month_num(m.group(2)), int(m.group(3))
                elif label == "day_noyear":
                    mo, d = _month_num(m.group(1)), int(m.group(2))
                    y = ts.year if (mo, d) <= (ts.month, ts.day) else ts.year - 1
                elif label == "year":
                    y = int(m.group(1))
                    mo, d = 1, 1
                elif label == "iso_day":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif label == "ym_num":
                    y, mo = int(m.group(1)), int(m.group(2))
                    if not 1 <= mo <= 12:  # not a month — e.g. "2025 30"
                        continue
                    d = 1
                else:  # month
                    mo, y = _month_num(m.group(1)), int(m.group(2))
                    d = 1
                iso = _mk(y, mo, d)
            except (ValueError, TypeError):
                continue
            out.append({"span": (m.start(), m.end()), "iso": iso,
                        "surface": m.group(0), "granularity": label})
            taken.append((m.start(), m.end()))

    for rx, label in REL_PATTERNS:
        for m in rx.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            r = resolve_relative(label, m, ts)
            if r:
                iso, surface = r
                out.append({"span": (m.start(), m.end()), "iso": iso,
                            "surface": m.group(0), "granularity": label})
                taken.append((m.start(), m.end()))

    for m in LAST_WEEKDAY.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        wd = WEEKDAYS.index(m.group(1).lower())
        delta = (ts.weekday() - wd) % 7 or 7
        d = ts - timedelta(days=delta)
        out.append({"span": (m.start(), m.end()), "iso": _mk(d.year, d.month, d.day),
                    "surface": m.group(0), "granularity": "weekday"})
        taken.append((m.start(), m.end()))

    out.sort(key=lambda d: d["span"][0])
    return out


def first_date(text: str, ts: datetime) -> str | None:
    ds = find_dates(text, ts)
    return ds[0]["iso"] if ds else None
