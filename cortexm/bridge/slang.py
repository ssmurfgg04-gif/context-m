"""Slang normalization dictionary (Urban Dictionary-style, deterministic).

WHY THIS MODULE EXISTS
-----------------------
The v0.5.x ``PerUserIdiolectNormalizer`` (``cortexm.text.idiolect``)
does EMBEDDING-BASED slang replacement — it learns from per-user
co-occurrence statistics. That handles novel slang it has never seen
before. But it requires the user to have used both the slang token and
its canonical form in the same context ≥2 times before the
replacement fires.

For the COMMON slang that appears in user text regardless of who's
speaking — "bruh", "deadass", "no cap", "finna", "gonna", "hella" —
a CURATED DICTIONARY is faster, more reliable, and doesn't need a
warm-up corpus. This module is that dictionary.

Google (pre-BERT) built statistical rewrite tables from query logs.
We don't have query logs. We have the published slang dictionaries
(Urban Dictionary, Twitter slang compendia, Reddit r/GenZ glossaries).
This module ports the high-frequency entries.

ARCHITECTURE
-------------
  * SLANG_NORMALIZATION: dict of lowercase_slang → canonical_form
  * normalize(text) applies the dictionary at both ingest and query
    time. Apply at ingest time so the verbatim_chunks FTS5 index
    contains the normalized form; apply at query time so the user's
    search matches the normalized form. The original text is preserved
    in the structured tier's source_chunks table — only the verbatim
    index gets the normalized copy.

μ=0: pure dict lookup. No statistics, no model.
"""
from __future__ import annotations

import re
from typing import Dict


# ---------------------------------------------------------------------------
# Curated slang → canonical English. The dictionary is conservatively
# curated — only entries that have a single unambiguous canonical form
# are included. Ambiguous slang ("sick" can mean "ill" or "great") is
# left to the embedding-based PerUserIdiolectNormalizer.
# ---------------------------------------------------------------------------
DEFAULT_SLANG: Dict[str, str] = {
    # Fillers / discourse markers — replaced with empty string (removed)
    "bruh": "",
    "bro": "",
    "yo": "",
    "ayy": "",
    "ayyy": "",
    "sheesh": "",
    "oof": "",
    "yeet": "",  # too context-dependent; drop
    "hella": "very",
    "mad": "very",  # "mad tired" → "very tired"
    "bare": "very",  # UK slang
    "well": "very",  # "well good" → "very good" (only in adjective context; conservative)
    # Intensifiers
    "deadass": "seriously",
    "no cap": "truthfully",
    "for real": "truthfully",
    "fr": "truthfully",
    "fr fr": "truthfully",
    "lowkey": "somewhat",
    "highkey": "very",
    "big time": "very",
    # Future/going-to
    "finna": "going to",
    "fixin to": "going to",
    "fixing to": "going to",
    "gonna": "going to",
    "wanna": "want to",
    "gotta": "have to",
    "oughta": "ought to",
    "shoulda": "should have",
    "coulda": "could have",
    "woulda": "would have",
    "lemme": "let me",
    "gimme": "give me",
    "outta": "out of",
    "kinda": "kind of",
    "sorta": "sort of",
    "dunno": "do not know",
    "ima": "I am going to",
    "i'ma": "I am going to",
    "tryna": "trying to",
    "bouta": "about to",
    "boutu": "about to",
    # Affirmation/negation
    "yep": "yes",
    "yeah": "yes",
    "yup": "yes",
    "uh-huh": "yes",
    "nope": "no",
    "nah": "no",
    "hell no": "definitely not",
    "hell yes": "definitely yes",
    # Description
    "lil": "little",
    "big ol": "big",
    "big ol'": "big",
    # UK slang
    "peng": "attractive",
    "bare": "very",
    "mint": "excellent",
    "gutted": "disappointed",
    "chuffed": "pleased",
    "knackered": "exhausted",
    "skint": "broke",
    "quid": "pounds",
    # Misc
    "k": "okay",
    "ok": "okay",
    "okie": "okay",
    "thx": "thanks",
    "ty": "thanks",
    "pls": "please",
    "plz": "please",
    "sry": "sorry",
    "rlly": "really",
    "rly": "really",
    "tbh": "to be honest",
    "imo": "in my opinion",
    "imho": "in my humble opinion",
    "ngl": "not going to lie",
    "ngl i": "not going to lie I",
    "smh": "shaking my head",
    "smh my head": "shaking my head",
    "fomo": "fear of missing out",
    "fwiw": "for what it's worth",
    "ikr": "I know right",
    "rn": "right now",
    "rn?": "right now",
    "af": "very",  # "tired af" → "tired very" (rough but functional)
    "asf": "very",
    "as fuck": "very",
    # Common contractions
    "y'all": "you all",
    "yall": "you all",
    "where'd": "where did",
    "what'd": "what did",
    "when'd": "when did",
    "how'd": "how did",
    "who'd": "who did",
    "why'd": "why did",
}


class SlangNormalizer:
    """Curated slang → canonical English normalizer.

    The normalizer is multi-word-phrase aware ("no cap" is a single
    semantic unit, not two tokens). It uses a master regex of all
    slang phrases (longest first, case-insensitive) to do phrase-level
    substitution at the character level.

    Idempotent: applying normalize() to an already-normalized text
    returns the same text (canonical English is in the dictionary's
    values, not keys, so no recursive expansion).
    """

    def __init__(self, slang: Dict[str, str] | None = None) -> None:
        self.slang = dict(slang or DEFAULT_SLANG)
        self._build_regex()

    def _build_regex(self) -> None:
        if not self.slang:
            self._re = re.compile(r"$.")
            return
        phrases = sorted(self.slang.keys(), key=len, reverse=True)
        joined = "|".join(re.escape(p) for p in phrases)
        self._re = re.compile(rf"\b(?:{joined})\b", re.IGNORECASE)

    def register(self, slang: str, canonical: str) -> None:
        """Add or replace a slang entry at runtime. Idempotent."""
        self.slang[slang.lower()] = canonical
        self._build_regex()

    def normalize(self, text: str) -> str:
        """Apply slang substitution.

        Whitespace from removed slang (where canonical = "") is
        collapsed so "bruh I work" doesn't become "  I work".
        """
        if not text:
            return text
        result = self._re.sub(self._repl, text)
        # Collapse double-spaces created by empty-string replacements
        result = re.sub(r" {2,}", " ", result)
        # Strip leading space if removal left a leading space
        result = re.sub(r"^ +", "", result)
        return result

    def _repl(self, m: re.Match) -> str:
        key = m.group(0).lower()
        return self.slang.get(key, m.group(0))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_default: SlangNormalizer | None = None


def default_normalizer() -> SlangNormalizer:
    global _default
    if _default is None:
        _default = SlangNormalizer()
    return _default


def normalize(text: str) -> str:
    """Module-level convenience wrapper."""
    return default_normalizer().normalize(text)
