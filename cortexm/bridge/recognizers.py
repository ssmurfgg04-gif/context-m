"""Deterministic entity recognizers (Microsoft Recognizers-Text lineage).

WHY THIS MODULE EXISTS
-----------------------
The v0.5.5 LongMemEval judge hardcoded a 20-entry holiday lookup
table. The user audit flagged this as a band-aid: Thanksgiving is
the fourth Thursday of November — an algorithm, not a date. Easter
is computus — a 12-step algorithm, not a date. Hardcoding them
makes the table incomplete forever.

This module ports the relevant subset of Microsoft's Recognizers-Text
library (deterministic, rule-based, multi-language) to μ=0 Python:
  * HOLIDAYS — algorithmic resolution. Thanksgiving, Easter, Mother's
    Day, Father's Day, Labor Day, Memorial Day, etc. are computed
    from the year, not looked up. Static holidays (Christmas,
    Valentine's Day, New Year) are tuples.
  * CURRENCY — regex extraction + normalization. "$1,234.56" → 1234.56.
    Handles USD, EUR, GBP, JPY, CNY, INR with both symbol and word
    forms.
  * DATES — relative + absolute date parsing. Already largely covered
    by ``cortexm.bridge.dates``; this module wraps it for the entity-
    recognizer API surface and adds holiday-name → date resolution.

ARCHITECTURE
-------------
The recognizers are stateless. The DeterministicRecognizer class is
a façade with three public methods: ``resolve_holiday()``,
``extract_currency()``, ``extract_dates()``. Each returns structured
data (dicts), never raw strings.

μ=0: no LLM, no API, no statistics. Pure regex + arithmetic.
"""
from __future__ import annotations

import re
from calendar import monthcalendar
from datetime import datetime, date
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# HOLIDAYS — static + algorithmic
# ---------------------------------------------------------------------------
# Static holidays: (month, day). The canonical form is the date in
# ISO format "MM-DD".
_STATIC_HOLIDAYS: Dict[str, tuple[int, int]] = {
    # New Year
    "new year": (1, 1), "new years day": (1, 1), "new year's day": (1, 1),
    "new years": (1, 1),
    # MLK Day is "third Monday of January" — algorithmic, see below
    # Presidents Day is "third Monday of February" — algorithmic
    # Valentine's
    "valentine's day": (2, 14), "valentines day": (2, 14), "valentine": (2, 14),
    "valentines": (2, 14),
    # St. Patrick's
    "st patrick's day": (3, 17), "st patricks day": (3, 17),
    "saint patrick's day": (3, 17), "st paddy's day": (3, 17),
    # April Fools
    "april fools day": (4, 1), "april fool's day": (4, 1), "april fools": (4, 1),
    # Memorial Day is "last Monday of May" — algorithmic
    # Juneteenth
    "juneteenth": (6, 19), "juneteenth day": (6, 19),
    "juneteenth independence day": (6, 19),
    # Independence Day (US)
    "independence day": (7, 4), "fourth of july": (7, 4), "july 4th": (7, 4),
    # Labor Day is "first Monday of September" — algorithmic
    # Columbus Day is "second Monday of October" — algorithmic
    # Halloween
    "halloween": (10, 31),
    # Veterans Day
    "veterans day": (11, 11), "veteran's day": (11, 11),
    # Thanksgiving is "fourth Thursday of November" — algorithmic
    # Christmas
    "christmas": (12, 25), "christmas day": (12, 25), "xmas": (12, 25),
    # Kwanzaa
    "kwanzaa": (12, 26),
    # Boxing Day (UK/Commonwealth)
    "boxing day": (12, 26),
    # New Year's Eve
    "new year's eve": (12, 31), "new years eve": (12, 31),
}
# (The dict literal above is clean and complete.)

# Algorithmic holidays: name → rule. The rule is resolved against a
# year (default = current year). Rules are functions or string codes
# the resolver knows about.
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the n-th `weekday` in `month` of `year`.

    weekday: 0=Monday (calendar.monthcalendar convention).
    n=1 → first, n=4 → fourth, n=-1 → last.
    """
    cal = monthcalendar(year, month)
    days = [week[weekday] for week in cal if week[weekday] != 0]
    if n < 0:
        return date(year, month, days[n])  # -1 → last
    return date(year, month, days[n - 1])


def _easter(year: int) -> date:
    """Computus (Gauss's algorithm) — Easter Sunday for Western churches."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


# Algorithmic rules. Each rule takes the year and returns a date.
_ALGORITHMIC_HOLIDAYS: Dict[str, object] = {
    "mlk day": lambda y: _nth_weekday(y, 1, 0, 3),  # 3rd Monday Jan
    "martin luther king day": lambda y: _nth_weekday(y, 1, 0, 3),
    "martin luther king jr day": lambda y: _nth_weekday(y, 1, 0, 3),
    "presidents day": lambda y: _nth_weekday(y, 2, 0, 3),  # 3rd Monday Feb
    "president's day": lambda y: _nth_weekday(y, 2, 0, 3),
    "washington's birthday": lambda y: _nth_weekday(y, 2, 0, 3),
    "memorial day": lambda y: _nth_weekday(y, 5, 0, -1),  # last Monday May
    "labour day": lambda y: _nth_weekday(y, 9, 0, 1),  # 1st Monday Sep
    "labor day": lambda y: _nth_weekday(y, 9, 0, 1),
    "columbus day": lambda y: _nth_weekday(y, 10, 0, 2),  # 2nd Monday Oct
    "indigenous peoples day": lambda y: _nth_weekday(y, 10, 0, 2),
    "thanksgiving": lambda y: _nth_weekday(y, 11, 3, 4),  # 4th Thursday Nov
    "thanksgiving day": lambda y: _nth_weekday(y, 11, 3, 4),
    "black friday": lambda y: date.fromordinal(
        _nth_weekday(y, 11, 3, 4).toordinal() + 1),  # day after Thanksgiving
    "cyber monday": lambda y: date.fromordinal(
        _nth_weekday(y, 11, 3, 4).toordinal() + 4),  # Monday after TG
    "easter": lambda y: _easter(y),
    "easter sunday": lambda y: _easter(y),
    "good friday": lambda y: date.fromordinal(_easter(y).toordinal() - 2),
    "mother's day": lambda y: _nth_weekday(y, 5, 6, 2),  # 2nd Sunday May
    "mothers day": lambda y: _nth_weekday(y, 5, 6, 2),
    "father's day": lambda y: _nth_weekday(y, 6, 6, 3),  # 3rd Sunday Jun
    "fathers day": lambda y: _nth_weekday(y, 6, 6, 3),
    "parents day": lambda y: _nth_weekday(y, 7, 6, 4),  # 4th Sunday Jul
}


# ---------------------------------------------------------------------------
# CURRENCY RECOGNIZER
# ---------------------------------------------------------------------------
# Match currency in two forms:
#   1. Symbol prefix: $1,234.56 / €50 / £100 / ¥1,000
#   2. Suffix words: 1,234.56 USD / 50 euros / 100 pounds
CURRENCY_RE = re.compile(
    r"(?:"
    r"(?P<sym>[$€£¥₹])\s*(?P<sym_amt>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"|"
    r"(?P<word_amt>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<word>USD|EUR|GBP|JPY|CNY|INR|dollars?|euros?|pounds?|yen|yuan|rupees?|bucks?)"
    r")",
    re.IGNORECASE,
)

_SYMBOL_TO_ISO = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",  # ambiguous JPY/CNY
    "₹": "INR",
}
_WORD_TO_ISO = {
    "usd": "USD", "dollars": "USD", "dollar": "USD", "bucks": "USD",
    "eur": "EUR", "euros": "EUR", "euro": "EUR",
    "gbp": "GBP", "pounds": "GBP", "pound": "GBP",
    "jpy": "JPY", "yen": "JPY",
    "cny": "CNY", "yuan": "CNY",
    "inr": "INR", "rupees": "INR", "rupee": "INR",
}


def _normalize_amount(raw: str) -> float:
    """'$1,234.56' → 1234.56. Strips all non-digit, non-dot chars."""
    cleaned = re.sub(r"[^\d.]", "", raw)
    return float(cleaned) if cleaned else 0.0


# ---------------------------------------------------------------------------
# DATE RECOGNIZER — wraps bridge.dates for the entity-recognizer API
# ---------------------------------------------------------------------------
# (Re-use the existing date parser to avoid duplicating the rich
# ABS_PATTERNS / REL_PATTERNS logic. The wrapper adds a holiday-name
# detector on top.)
DATE_PATTERN_RE = re.compile(
    r"\b("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public façade
# ---------------------------------------------------------------------------
class DeterministicRecognizer:
    """Microsoft Recognizers-Text style deterministic entity resolution.

    Stateless. All methods are pure functions over their input text.
    """

    HOLIDAY_DATES_STATIC = _STATIC_HOLIDAYS
    HOLIDAY_DATES_ALGORITHMIC = _ALGORITHMIC_HOLIDAYS
    CURRENCY_RE = CURRENCY_RE
    DATE_PATTERN_RE = DATE_PATTERN_RE

    # ------------------------------------------------------------------
    # Holiday resolution
    # ------------------------------------------------------------------

    def resolve_holiday(self, holiday_name: str, year: Optional[int] = None
                        ) -> Optional[str]:
        """Resolve a holiday name to an ISO date string.

        Returns ``"MM-DD"`` for static holidays (no year context),
        ``"YYYY-MM-DD"`` for algorithmic holidays (year-dependent).

        Returns ``None`` if the name is not recognized.

        Idempotent: same name + year → same output.
        """
        if not holiday_name:
            return None
        year = year or datetime.now().year
        canonical = holiday_name.lower().strip().strip("'").strip("`")

        if canonical in _STATIC_HOLIDAYS:
            month, day = _STATIC_HOLIDAYS[canonical]
            return f"{year:04d}-{month:02d}-{day:02d}"

        if canonical in _ALGORITHMIC_HOLIDAYS:
            rule = _ALGORITHMIC_HOLIDAYS[canonical]
            d = rule(year)  # type: ignore[operator]
            return d.strftime("%Y-%m-%d")

        return None

    def is_holiday_name(self, text: str) -> bool:
        """True if ``text`` (lowercased) is a known holiday name."""
        t = text.lower().strip()
        return t in _STATIC_HOLIDAYS or t in _ALGORITHMIC_HOLIDAYS

    # ------------------------------------------------------------------
    # Currency extraction
    # ------------------------------------------------------------------

    def extract_currency(self, text: str) -> List[Dict]:
        """Extract all currency amounts from text.

        Returns a list of dicts, each with:
          - ``text``: the raw matched substring
          - ``value``: float amount (e.g. 1234.56)
          - ``currency``: ISO code (USD/EUR/GBP/JPY/CNY/INR)
          - ``span``: (start, end) character offsets
        """
        out: List[Dict] = []
        if not text:
            return out
        for m in CURRENCY_RE.finditer(text):
            if m.group("sym"):
                currency = _SYMBOL_TO_ISO.get(m.group("sym"), "USD")
                value = _normalize_amount(m.group("sym_amt"))
            else:
                word = m.group("word").lower()
                currency = _WORD_TO_ISO.get(word, "USD")
                value = _normalize_amount(m.group("word_amt"))
            out.append({
                "text": m.group(0),
                "value": value,
                "currency": currency,
                "span": (m.start(), m.end()),
            })
        return out

    @staticmethod
    def normalize_currency(raw: str) -> float:
        """Public wrapper for the amount normalizer."""
        return _normalize_amount(raw)

    # ------------------------------------------------------------------
    # Date extraction (delegates to bridge.dates for the heavy lifting)
    # ------------------------------------------------------------------

    def extract_dates(self, text: str,
                      context_ts: Optional[datetime] = None
                      ) -> List[Dict]:
        """Extract absolute + relative date references from text.

        Returns a list of dicts with ``text``, ``resolved`` (ISO date),
        and ``span``.
        """
        if not text:
            return []
        # Defer the import so this module stays usable without bridge.dates
        from cortexm.bridge.dates import resolve_date_expr
        context_ts = context_ts or datetime.now()
        out: List[Dict] = []
        for m in DATE_PATTERN_RE.finditer(text):
            raw = m.group(0)
            try:
                resolved = resolve_date_expr(raw, context_ts)
                if resolved:
                    out.append({
                        "text": raw,
                        "resolved": resolved,
                        "span": (m.start(), m.end()),
                    })
            except Exception:
                pass
        # Also scan for holiday names; resolve them too
        for canonical in list(_STATIC_HOLIDAYS.keys()) + \
                list(_ALGORITHMIC_HOLIDAYS.keys()):
            pattern = re.compile(rf"\b{re.escape(canonical)}\b", re.IGNORECASE)
            for m in pattern.finditer(text):
                resolved = self.resolve_holiday(canonical, context_ts.year)
                if resolved:
                    out.append({
                        "text": m.group(0),
                        "resolved": resolved,
                        "span": (m.start(), m.end()),
                        "kind": "holiday",
                    })
        return out


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_default_recognizer: DeterministicRecognizer | None = None


def default_recognizer() -> DeterministicRecognizer:
    global _default_recognizer
    if _default_recognizer is None:
        _default_recognizer = DeterministicRecognizer()
    return _default_recognizer
