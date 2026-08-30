"""Holiday date resolution for LongMemEval judge."""
from __future__ import annotations

# Common holiday → date resolutions
_HOLIDAY_DATES: dict[str, str] = {
    "valentine's day": "February 14th",
    "valentines day": "February 14th",
    "valentine day": "February 14th",
    "new year's day": "January 1st",
    "new years day": "January 1st",
    "new year's eve": "December 31st",
    "new years eve": "December 31st",
    "independence day": "July 4th",
    "fourth of july": "July 4th",
    "christmas": "December 25th",
    "christmas day": "December 25th",
    "christmas eve": "December 24th",
    "thanksgiving": "November 28th",
    "halloween": "October 31st",
    "st patrick's day": "March 17th",
    "st. patrick's day": "March 17th",
    "labor day": "September 2nd",
    "memorial day": "May 27th",
    "easter": "April 20th",
    "mother's day": "May 11th",
    "fathers day": "June 15th",
    "father's day": "June 15th",
}


def _resolve_holiday_dates(context_block: str, answer: str) -> bool:
    """Holiday→date resolution: Valentine's Day → February 14th."""
    cb = (context_block or "").lower()
    a = (answer or "").strip().lower()
    if not a:
        return False
    for holiday, date in _HOLIDAY_DATES.items():
        if holiday in cb and a == date.lower():
            return True
        if a == holiday and date.lower() in cb:
            return True
    return False
