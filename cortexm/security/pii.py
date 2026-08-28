"""PII detection, redaction, and reversible tokenization (GDPR/CCPA).

Enterprises cannot ship a memory layer that spreads personal data across
every table. This module runs on the WRITE path (before extraction), so
raw PII never reaches the Trace, the Palace, or the vector codec — the
deterministic extractor only ever sees surrogate tokens.

Modes:
  off     — pass-through (development default; μ=0 benchmarks use this)
  redact  — replace spans with typed tokens ``«PII:EMAIL:7f3a»``; the
            mapping lives in a vault (encrypted at rest when a master
            key is configured) so authorised re-identification stays
            possible (e.g. DSAR subject-access requests)
  block   — refuse the message entirely (strict exfiltration guard)

Detectors are regex + checksum based (Luhn for cards, mod-97 for IBAN,
SSN area/group rules) — zero LLM calls, keeping the μ=0 protocol intact.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

# ---------------------------------------------------------------- detectors
_LUHN_OK: set[str] = set()  # memoized card prefixes that pass Luhn


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(s: str) -> bool:
    s = re.sub(r"\s", "", s).upper()
    if len(s) < 15 or not s[:2].isalpha() or not s[2:].isdigit() or len(s) > 34:
        return False
    rearranged = s[4:] + s[:4]
    total = 0
    for ch in rearranged:
        total = (total * (36 if ch.isalpha() else 10) +
                 (ord(ch) - (55 if ch.isalpha() else 48))) % 97
    return total == 1


def _ssn_ok(s: str) -> bool:
    digits = re.sub(r"\D", "", s)
    if len(digits) != 9:
        return False
    area, group, serial = int(digits[0:3]), int(digits[3:5]), int(digits[5:9])
    return not (area in (0, 666) or area >= 900) and group != 0 and serial != 0


@dataclass
class Span:
    kind: str
    start: int
    end: int
    text: str
    token: str = ""


DETECTORS: list[tuple[str, re.Pattern]] = [
    # order matters: earlier patterns claim overlapping spans first
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?"
                         r"\d{3,4}[\s.-]?\d{3,4}(?:[\s.-]?\d{2,4})?\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                      r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("API_KEY", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}|"
                           r"gho_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}|"
                           r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})\b")),
    ("PASSPORT", re.compile(r"\b[A-Z]{1,2}\d{6,9}\b(?![\w-])")),
]

_CHECKS = {"CREDIT_CARD": lambda t: _luhn_ok(re.sub(r"[ -]", "", t)),
           "IBAN": _iban_ok,
           "SSN": _ssn_ok,
           "IP": lambda t: True}


def scan(text: str) -> list[Span]:
    """All validated PII spans in ``text``, left-to-right, non-overlapping."""
    out: list[Span] = []
    taken: list[tuple[int, int]] = []

    def overlaps(s: int, e: int) -> bool:
        return any(not (e <= a or s >= b) for a, b in taken)

    for kind, rx in DETECTORS:
        for m in rx.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            raw = m.group(0)
            check = _CHECKS.get(kind)
            # card-shaped spans are claimed even on checksum failure —
            # otherwise their digits get reinterpreted as a phone number
            claim = kind == "CREDIT_CARD"
            if claim:
                taken.append((m.start(), m.end()))
            if check and not check(raw):
                continue
            # phone sanity: needs >= 7 digits total and a separator/space or +
            if kind == "PHONE":
                digits = re.sub(r"\D", "", raw)
                if len(digits) < 7 or len(digits) > 15:
                    continue
                if not re.search(r"[+\s().-]", raw) and len(digits) <= 9 \
                        and not raw.startswith("+"):
                    continue  # bare short number — probably an id, not a phone
            out.append(Span(kind, m.start(), m.end(), raw))
            taken.append((m.start(), m.end()))
    out.sort(key=lambda s: s.start)
    return out


# ---------------------------------------------------------------- vault
class PIIVault:
    """Reversible token vault. ``token -> original`` lives in the kv store,
    optionally encrypted with the master key (AES-256-GCM). Crypto-shredding
    the vault key renders every token permanently unrecoverable."""

    def __init__(self, store, cipher=None) -> None:
        self.store = store          # TraceStore (kv table)
        self.cipher = cipher        # cortexm.security.crypto.AESGCMCipher | None
        self._counter_key = "pii:vault:counter"

    def _next_id(self) -> str:
        cur = int(self.store.kv_get(self._counter_key, "0") or "0")
        self.store.kv_set(self._counter_key, str(cur + 1))
        return f"{cur + 1:04x}"

    def tokenize(self, kind: str, original: str) -> str:
        """Store original, return stable token. Deterministic per original
        (same value -> same token) so contradictions still resolve."""
        digest = None
        raw_index = self.store.kv_get("pii:vault:index") or "{}"
        import json
        try:
            index = json.loads(raw_index)
        except Exception:
            index = {}
        # index maps sha256-ish hash -> token id (avoid storing plaintext key)
        from cortexm.security.hashes import HashProvider
        h = HashProvider("blake2b").hash_text(original)[:24]
        if h in index:
            return f"«PII:{kind}:{index[h]}»"
        tid = self._next_id()
        index[h] = tid
        self.store.kv_set("pii:vault:index", json.dumps(index))
        payload = original
        if self.cipher is not None:
            payload = self.cipher.encrypt_str(original)
        self.store.kv_set(f"pii:vault:{tid}", payload)
        return f"«PII:{kind}:{tid}»"

    def resolve(self, token: str) -> str | None:
        """Token -> original (DSAR / subject-access path)."""
        m = re.fullmatch(r"«PII:(\w+):([0-9a-f]+)»", token)
        if not m:
            return None
        payload = self.store.kv_get(f"pii:vault:{m.group(2)}")
        if payload is None:
            return None
        if self.cipher is not None and not payload.startswith("«enc:"):
            return None  # vault not encrypted but cipher set — mismatch
        if self.cipher is not None:
            return self.cipher.decrypt_str(payload)
        return payload

    def crypto_shred(self) -> int:
        """Destroy recoverability of every vault entry (GDPR erasure).
        Returns number of tokens shredded."""
        import json
        raw_index = self.store.kv_get("pii:vault:index") or "{}"
        try:
            index = json.loads(raw_index)
        except Exception:
            index = {}
        n = 0
        for _h, tid in index.items():
            if self.store.kv_get(f"pii:vault:{tid}") is not None:
                n += 1
            self.store.kv_set(f"pii:vault:{tid}",
                              "«shredded»" if self.cipher is None
                              else self.cipher.encrypt_str("«shredded»"))
        # rotate the index so old tokens cannot be re-linked
        self.store.kv_set("pii:vault:index", "{}")
        self.store.kv_set(self._counter_key, "0")
        return n

    def stats(self) -> dict:
        import json
        raw_index = self.store.kv_get("pii:vault:index") or "{}"
        try:
            n = len(json.loads(raw_index))
        except Exception:
            n = 0
        return {"vault_entries": n, "encrypted": self.cipher is not None}


# ---------------------------------------------------------------- engine
@dataclass
class PIIResult:
    mode: str
    blocked: bool = False
    redacted_text: str = ""
    spans: list[Span] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)


class PIIGuard:
    """Write-path PII policy engine."""

    def __init__(self, mode: str = "off", vault: PIIVault | None = None) -> None:
        if mode not in ("off", "redact", "block", "tag"):
            raise ValueError(f"unknown pii mode {mode!r}")
        self.mode = mode
        self.vault = vault

    def process(self, text: str) -> PIIResult:
        res = PIIResult(mode=self.mode, redacted_text=text)
        if self.mode == "off" or not text:
            return res
        res.spans = scan(text)
        if not res.spans:
            return res
        if self.mode == "block":
            res.blocked = True
            return res
        if self.mode == "tag":
            res.tokens = [f"{s.kind}" for s in res.spans]
            return res
        # redact: right-to-left replacement so indices stay valid
        out = text
        for s in reversed(res.spans):
            if self.vault is not None:
                token = self.vault.tokenize(s.kind, s.text)
                s.token = token
                res.tokens.append(token)
                out = out[:s.start] + token + out[s.end:]
            else:
                out = out[:s.start] + f"«PII:{s.kind}»" + out[s.end:]
        res.redacted_text = out
        return res


def redact_inplace(text: str) -> str:
    """Stateless helper: mask without vault (irreversible)."""
    out = text
    for s in reversed(scan(text)):
        out = out[:s.start] + f"«PII:{s.kind}»" + out[s.end:]
    return out
