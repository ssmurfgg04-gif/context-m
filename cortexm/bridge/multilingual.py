"""Language detection + per-language routing (Google multi-analyzer lineage).

WHY THIS MODULE EXISTS
-----------------------
The v0.5.x ``HashingEmbedder`` + ``PolyglotEncoder`` fallback handles
non-English text at the EMBEDDING level — a Spanish query and a
Spanish chunk end up in the same vector space because both go through
the polyglot encoder. But the EXTRACTOR and BM25 still treat non-
English as English: patterns don't fire, FTS5 tokenization is sub-
optimal for CJK (no word boundaries), and the structured tier is
silent on non-English text.

Google (pre-BERT) used per-language analyzers: fastText language
identification → route to the language-specific tokenizer/stemmer.
We don't ship fastText (the lid.176.ftz model is 1 MB but the wheel
adds weight; let's stay μ=0 + stdlib-only). Instead, this module
implements a SCRIPT-BASED language detector: Unicode script analysis
(CJK / Devanagari / Arabic / Cyrillic / Thai / Hangul / Hiragana /
Katakana / Latin) is sufficient for routing and is O(text length).

ARCHITECTURE
-------------
  * ``detect_language(text)`` returns a 2-letter ISO code
    ("en", "zh", "ja", "ko", "ar", "hi", "ru", "th", "mix")
  * ``segment_by_language(text)`` splits code-switched text into
    language-homogeneous segments
  * ``LanguageAwareProcessor.process(text)`` returns
    ``{"lang": ..., "text": ..., "skip_extraction": bool}`` — the
    Memory writer checks ``skip_extraction`` to decide whether to
    route to the structured extractor (English) or skip it and go
    verbatim-only (non-English).

μ=0: pure Unicode script analysis + dict lookup. No model, no API.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List


# ---------------------------------------------------------------------------
# Unicode script ranges (simplified — covers the BMP, good enough for
# routing; supplementary-plane chars like emoji are classified as
# "Latin" because they don't match any non-Latin block, which is fine
# since emoji don't carry semantic content for retrieval).
# ---------------------------------------------------------------------------
def _script(ch: str) -> str:
    """Return the script family for a single character."""
    cp = ord(ch)
    if cp == 0x20:
        return "SPACE"
    # CJK Unified Ideographs (Chinese + Japanese Kanji)
    if 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
        return "CJK"
    # Hiragana
    if 0x3040 <= cp <= 0x309F:
        return "HIRAGANA"
    # Katakana
    if 0x30A0 <= cp <= 0x30FF or 0xFF66 <= cp <= 0xFF9F:
        return "KATAKANA"
    # Hangul (Korean) — syllables + jamo
    if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or \
            0x3130 <= cp <= 0x318F:
        return "HANGUL"
    # Arabic + Arabic Extended
    if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or \
            0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
        return "ARABIC"
    # Devanagari (Hindi + Marathi + Nepali)
    if 0x0900 <= cp <= 0x097F:
        return "DEVANAGARI"
    # Cyrillic (Russian + Ukrainian + Bulgarian + Serbian)
    if 0x0400 <= cp <= 0x04FF or 0x0500 <= cp <= 0x052F:
        return "CYRILLIC"
    # Thai
    if 0x0E00 <= cp <= 0x0E7F:
        return "THAI"
    # Greek
    if 0x0370 <= cp <= 0x03FF:
        return "GREEK"
    # Hebrew
    if 0x0590 <= cp <= 0x05FF:
        return "HEBREW"
    # Latin (default for ASCII + Latin-1 + Latin Extended)
    cat = unicodedata.category(ch)
    if cat.startswith("L") or cat.startswith("N"):
        return "LATIN"
    return "OTHER"


# Map script-family → ISO 639-1 language code. "CJK" is ambiguous
# (Chinese vs Japanese Kanji) — we resolve via hiragana/katakana
# presence (Japanese has them, pure Chinese doesn't).
_SCRIPT_TO_LANG = {
    "HIRAGANA": "ja",
    "KATAKANA": "ja",
    "HANGUL": "ko",
    "ARABIC": "ar",
    "DEVANAGARI": "hi",
    "CYRILLIC": "ru",
    "THAI": "th",
    "GREEK": "el",
    "HEBREW": "he",
}


def detect_language(text: str) -> str:
    """Detect the dominant language of text.

    Returns a 2-letter ISO 639-1 code:
      "en" — Latin script, no other script present in significant amount
      "zh" — CJK only (no hiragana/katakana)
      "ja" — CJK + (hiragana or katakana)
      "ko" — Hangul
      "ar" — Arabic
      "hi" — Devanagari
      "ru" — Cyrillic
      "th" — Thai
      "el" — Greek
      "he" — Hebrew
      "mix" — multiple non-Latin scripts co-dominant (code-switching)

    The detector is INTENTIONALLY conservative: if the text is mostly
    Latin with a few CJK chars (e.g. "I love 東京"), it returns "en"
    because the structured English extractor + verbatim tier will
    still find the chunk. Only when the non-Latin script dominates
    does the detector return a non-English code, signaling the
    caller to skip the English pattern extractor.
    """
    if not text:
        return "en"
    counts: Dict[str, int] = {}
    for ch in text:
        s = _script(ch)
        if s in ("SPACE", "OTHER"):
            continue
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return "en"
    # Find dominant non-Latin script (if any)
    non_latin = {s: c for s, c in counts.items() if s != "LATIN"}
    if not non_latin:
        return "en"
    dominant = max(non_latin, key=non_latin.get)
    # Need ≥30% of total chars to be dominant non-Latin; otherwise
    # the text is effectively Latin (a few embedded words like
    # "I love 東京" should route to the English extractor, not the
    # verbatim-only path). 30% is conservative — catches mixed-script
    # text without mislabeling English-with-a-few-foreign-words.
    total = sum(counts.values())
    if non_latin[dominant] / total < 0.30:
        return "en"
    # Check for code-switching: if Latin is also ≥20% of total, the
    # text is mixed. We still return the dominant non-Latin language
    # so the caller routes appropriately; the segmenter below can
    # split the Latin part out if needed.
    if "CJK" in non_latin and ("HIRAGANA" in non_latin or "KATAKANA" in non_latin):
        return "ja"
    if "CJK" in non_latin:
        return "zh"
    if dominant in _SCRIPT_TO_LANG:
        return _SCRIPT_TO_LANG[dominant]
    return "en"


def segment_by_language(text: str) -> List[Dict]:
    """Split code-switched text into language-homogeneous segments.

    Returns a list of ``{"lang": ..., "text": ...}`` dicts.

    Algorithm: walk the text token-by-token (whitespace split), detect
    language per token, and group consecutive same-language tokens into
    segments. Single tokens that mix scripts (e.g. "東京2026") go to
    the non-Latin bucket if they contain any non-Latin char.

    This is the segmenter used by the writer when it ingests a multi-
    language chunk: each segment goes into the verbatim index with
    its language tag, and only English segments go to the structured
    extractor.
    """
    if not text:
        return []
    out: List[Dict] = []
    cur_lang = None
    cur_words: List[str] = []
    # Use a regex split that preserves whitespace position so we can
    # re-join faithfully.
    tokens = re.findall(r"\S+|\s+", text)
    for tok in tokens:
        if tok.isspace():
            cur_words.append(tok)
            continue
        # Detect language for this token. Scan ALL chars (not just first
        # non-Latin) so a token like "私は" (Hiragana) isn't mislabeled
        # as Chinese because the first char "私" is CJK. If ANY char is
        # Hiragana or Katakana, the token is Japanese.
        tok_lang = "en"
        has_cjk = False
        has_japanese_kana = False
        has_other_non_latin = None  # script family
        for ch in tok:
            s = _script(ch)
            if s in ("SPACE", "OTHER", "LATIN"):
                continue
            if s == "CJK":
                has_cjk = True
            elif s in ("HIRAGANA", "KATAKANA"):
                has_japanese_kana = True
            elif s in _SCRIPT_TO_LANG:
                has_other_non_latin = s
        if has_japanese_kana:
            tok_lang = "ja"
        elif has_cjk:
            tok_lang = "zh"
        elif has_other_non_latin:
            tok_lang = _SCRIPT_TO_LANG[has_other_non_latin]
        if cur_lang is None:
            cur_lang = tok_lang
        if tok_lang != cur_lang:
            if cur_words:
                out.append({"lang": cur_lang,
                            "text": "".join(cur_words).strip()})
            cur_lang = tok_lang
            cur_words = [tok]
        else:
            cur_words.append(tok)
    if cur_words:
        out.append({"lang": cur_lang or "en",
                    "text": "".join(cur_words).strip()})
    # Filter out empty segments (whitespace-only)
    return [s for s in out if s["text"]]


class LanguageAwareProcessor:
    """Per-language routing façade.

    process(text) returns:
      - For English text: ``{"lang": "en", "text": text, "stemmed": None,
        "skip_extraction": False}`` — extractor runs normally.
      - For non-English text: ``{"lang": ..., "text": text,
        "stemmed": None, "skip_extraction": True}`` — extractor skipped,
        writer stores verbatim only with the language tag.

    Stemming is left to a future per-language Snowball wrapper; for
    now the verbatim tier's FTS5 unicode61 tokenizer handles the
    tokenization and the polyglot encoder handles the embedding.
    """

    def __init__(self) -> None:
        self.labse_enabled = True  # always route non-English through polyglot

    def process(self, text: str) -> Dict:
        if not text:
            return {"lang": "en", "text": "", "stemmed": None,
                    "skip_extraction": False}
        lang = detect_language(text)
        if lang == "en":
            return {"lang": "en", "text": text, "stemmed": None,
                    "skip_extraction": False}
        return {"lang": lang, "text": text, "stemmed": None,
                "skip_extraction": True}

    def segment(self, text: str) -> List[Dict]:
        """Public wrapper around segment_by_language."""
        return segment_by_language(text)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_default: LanguageAwareProcessor | None = None


def default_processor() -> LanguageAwareProcessor:
    global _default
    if _default is None:
        _default = LanguageAwareProcessor()
    return _default


def detect(text: str) -> str:
    """Module-level convenience: detect language."""
    return detect_language(text)
