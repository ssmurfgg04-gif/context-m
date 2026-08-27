"""Shared utilities: time, ids, normalization, string similarity."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

WORDS = re.compile(r"[a-z0-9']+")


def h64(feature: str, seed: int = 0) -> int:
    """Deterministic 64-bit hash of a string feature (stable across runs)."""
    import hashlib
    return int.from_bytes(
        hashlib.blake2b(feature.encode("utf-8"), digest_size=8,
                        key=(seed & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")).digest(),
        "little")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def normalize(text: str) -> str:
    """Lowercase, strip punctuation/articles, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s']+", " ", text)
    text = re.sub(r"\b(the|a|an|is|are|was|were)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def words(text: str) -> list[str]:
    return WORDS.findall(text.lower())


def token_estimate(text: str) -> int:
    """Cheap token estimator (~1.3 tokens/word). Deterministic."""
    n = len(text.split())
    return max(1, round(n * 1.3))


def levenshtein(a: str, b: str, cutoff: float = 1.0) -> int:
    """Levenshtein distance with early exit when distance exceeds cutoff*maxlen."""
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    maxd = int(max(la, lb) * cutoff) + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            best = min(best, cur[j])
        if best > maxd:
            return maxd
        prev = cur
    return prev[lb]


def similarity(a: str, b: str) -> float:
    """Combined token-Jaccard + Levenshtein string similarity in [0, 1].

    Fast paths: jaccard decides when clearly similar or clearly
    different; Levenshtein (with early-exit cutoff) only runs in the
    ambiguous band. Keeps conflict analysis linear-ish at scale.
    """
    a_n, b_n = normalize(a), normalize(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    wa, wb = set(words(a_n)), set(words(b_n))
    union = wa | wb
    jac = len(wa & wb) / len(union) if union else 0.0
    if jac >= 0.92:
        return jac
    if jac < 0.30:
        return jac
    d = levenshtein(a_n, b_n, cutoff=0.75)
    lev = 1.0 - d / max(len(a_n), len(b_n))
    return max(jac, lev)


def month_number(name: str) -> int | None:
    m = name.strip()[:3].lower()
    table = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    return table.get(m)


def month_name(num: int) -> str:
    return ["January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December"][num - 1]


def days_in(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - timedelta(days=1)).day


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def fmt_month(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
