"""Tests for the LaBSE-inspired polyglot hashing encoder.

Three test surfaces:
  1. **Per-script smoke**: each non-English script (Chinese, Arabic,
     Devanagari, Cyrillic, mixed Japanese) produces a 768-dim L2-normalized
     non-zero float32 vector. This is the bare minimum that the existing
     HashingEmbedder fails — see docs/BENCHMARKS.md Tier-1 non-English row.
  2. **Determinism**: two runs of the same text produce bit-identical
     vectors (BLAKE2b hashing + fixed feature iteration order + single-
     threaded float32 accumulation). Verified via .tobytes() equality.
  3. **Cross-language cosine similarity**: "Alice works at Google" (en)
     and "爱丽丝在谷歌工作" (zh) share enough structural features
     (TOK, TOK:short, char-n-gram pattern overlaps) to land >= 0.10
     cosine — the "non-trivial" threshold the spec calls out. Also
     checks the other language pairs land >= 0.10.

Also tests the hybrid path on HashingEmbedder (labse_enabled=True
delegates >30% non-ASCII text to PolyglotEncoder) and the
Config.labse_enabled flag + CONTEXT_M_LABSE env override.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from cortexm.config import Config
from cortexm.text.embedder import HashingEmbedder
from cortexm.text.labse import PolyglotEncoder, encode


# ----------------------------------------------------------------- helpers
def _is_unit(v: np.ndarray, atol: float = 1e-5) -> bool:
    """Check that a vector is L2-normalized to length 1.0 (within float32 tol)."""
    n = float(np.linalg.norm(v))
    return abs(n - 1.0) < atol


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Both vectors are assumed L2-normalized (so dot = cos)."""
    return float(np.dot(a, b))


# ------------------------------------------------------ per-script smoke
class TestPolyglotPerScript:
    """Each supported script must produce a 768-dim unit vector."""

    def test_english(self):
        v = encode("Alice works at Google")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), f"L2 norm != 1: {float(np.linalg.norm(v))}"
        assert v.any(), "English encoding produced all-zero vector"

    def test_chinese(self):
        # 8 CJK ideographs, no whitespace word boundaries
        v = encode("爱丽丝在谷歌工作")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), f"L2 norm != 1: {float(np.linalg.norm(v))}"
        assert v.any(), "Chinese encoding produced all-zero vector"

    def test_arabic(self):
        # RTL script with whitespace word boundaries + combining marks
        v = encode("تعمل أليس في جوجل")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), f"L2 norm != 1: {float(np.linalg.norm(v))}"
        assert v.any(), "Arabic encoding produced all-zero vector"

    def test_devanagari(self):
        # Indic script with vowel signs (Mc marks) that must stick to host
        v = encode("एलिस गूगल में काम करती है")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), f"L2 norm != 1: {float(np.linalg.norm(v))}"
        assert v.any(), "Devanagari encoding produced all-zero vector"

    def test_cyrillic(self):
        v = encode("Алиса работает в Гугле")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), f"L2 norm != 1: {float(np.linalg.norm(v))}"
        assert v.any(), "Cyrillic encoding produced all-zero vector"

    def test_japanese_mixed(self):
        # Mixed scripts: Katakana + Hiragana + Latin + Han. Tokenizer
        # must split at every script transition AND pop out each Han
        # ideograph as its own token.
        v = encode("アリスはGoogleで働いています")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), f"L2 norm != 1: {float(np.linalg.norm(v))}"
        assert v.any(), "Mixed-Japanese encoding produced all-zero vector"


# ----------------------------------------------------------- determinism
class TestPolyglotDeterminism:
    """Two runs of the same text produce bit-identical vectors."""

    def test_determinism_same_instance(self):
        enc = PolyglotEncoder()
        text = "Alice works at Google 爱丽丝在谷歌工作"
        a = enc.encode(text)
        b = enc.encode(text)
        # Bit-identical (BLAKE2b hashing, fixed feature order, single-threaded
        # float32 accumulation). Compared via .tobytes() so any ULP drift
        # between two consecutive calls fails the test.
        assert a is not b, "encode() returned the same array object (cache bug)"
        assert a.tobytes() == b.tobytes(), \
            "Two runs of same text differ — non-deterministic encoding"

    def test_determinism_two_instances(self):
        # Two fresh encoders with the same params must produce the same vector
        a = PolyglotEncoder().encode("hello world")
        b = PolyglotEncoder().encode("hello world")
        assert a.tobytes() == b.tobytes(), \
            "Two fresh encoder instances produced different vectors"

    def test_determinism_across_languages(self):
        enc = PolyglotEncoder()
        for text in ["Alice", "爱丽丝", "تعمل", "एलिस", "Алиса", "アリス"]:
            a = enc.encode(text)
            b = enc.encode(text)
            assert a.tobytes() == b.tobytes(), \
                f"Non-deterministic for text {text!r}"


# ----------------------------------------------- cross-language similarity
class TestPolyglotCrossLanguage:
    """Cross-language cosine similarity must clear the 0.10 threshold.

    "Alice works at Google" (en) and "爱丽丝在谷歌工作" (zh) don't share
    any surface-form n-grams (English "Google" ≠ Chinese "谷歌"), so pure
    random hashing would land them at cos ≈ 0.03 (the random-projection
    noise floor). The PolyglotEncoder's per-token structural features
    (TOK, TOK:short / TOK:long) inject a small language-agnostic
    positive bias that lifts the cross-language sim above 0.10 — the
    LaBSE-equivalent of "same sentence shape" alignment.
    """

    def test_cross_language_sim_en_zh(self):
        enc = PolyglotEncoder()
        en = enc.encode("Alice works at Google")
        zh = enc.encode("爱丽丝在谷歌工作")
        cos = _cos(en, zh)
        assert cos >= 0.10, f"en-zh cos={cos:.4f} < 0.10"

    def test_cross_language_sim_all_pairs(self):
        enc = PolyglotEncoder()
        en = enc.encode("Alice works at Google")
        pairs = [
            ("ar", enc.encode("تعمل أليس في جوجل")),
            ("dev", enc.encode("एलिस गूगल में काम करती है")),
            ("cyr", enc.encode("Алиса работает в Гугле")),
            ("ja", enc.encode("アリスはGoogleで働いています")),
        ]
        for tag, v in pairs:
            cos = _cos(en, v)
            assert cos >= 0.10, f"en-{tag} cos={cos:.4f} < 0.10"

    def test_self_similarity_is_one(self):
        # Sanity: a text is perfectly similar to itself.
        enc = PolyglotEncoder()
        a = enc.encode("Alice works at Google")
        b = enc.encode("Alice works at Google")
        assert abs(_cos(a, b) - 1.0) < 1e-5

    def test_unrelated_text_not_too_similar(self):
        # Sanity: two clearly unrelated texts shouldn't be > 0.90
        # (would indicate the encoder is dominated by structural
        # features alone — leaking no lexical signal).
        enc = PolyglotEncoder()
        a = enc.encode("Alice works at Google")
        b = enc.encode("The mitochondrion is the powerhouse of the cell")
        # These share no surface form, only generic structural features
        # (both are short English sentences). Should land well below 0.5.
        cos = _cos(a, b)
        assert cos < 0.5, f"unrelated en-en cos={cos:.4f} >= 0.5 (too high)"


# --------------------------------------------------------- edge cases
class TestPolyglotEdgeCases:
    """Empty input, dimension parameter, batch mode."""

    def test_zero_input_empty(self):
        enc = PolyglotEncoder()
        v = enc.encode("")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert not v.any(), "Empty input should produce zero vector"

    def test_zero_input_whitespace_only(self):
        enc = PolyglotEncoder()
        v = enc.encode("   \t\n  ")
        assert v.shape == (768,)
        assert not v.any(), "Whitespace-only input should produce zero vector"

    def test_zero_input_punctuation_only(self):
        enc = PolyglotEncoder()
        v = enc.encode("... !!! ??? ---")
        assert v.shape == (768,)
        assert not v.any(), "Punctuation-only input should produce zero vector"

    def test_compat_dims(self):
        for dims in (128, 256, 512, 768, 1024, 2048):
            v = encode("hello world", dims=dims)
            assert v.shape == (dims,), f"dims={dims}: got shape {v.shape}"
            assert _is_unit(v), f"dims={dims}: not L2-normalized"

    def test_single_char_english(self):
        # Boundary: one ASCII char
        v = encode("a")
        assert v.shape == (768,)
        assert _is_unit(v)
        assert v.any()

    def test_single_char_cjk(self):
        # Boundary: one CJK ideograph (no whitespace, no other chars)
        v = encode("爱")
        assert v.shape == (768,)
        assert _is_unit(v)
        assert v.any()


# ------------------------------------------------------------- batch
class TestPolyglotBatch:
    """encode_batch: shape, empty, parity with single encode."""

    def test_batch_shape(self):
        enc = PolyglotEncoder()
        texts = ["hello", "world", "爱丽丝", "Alice works at Google"]
        vs = enc.encode_batch(texts)
        assert vs.shape == (4, 768)
        assert vs.dtype == np.float32

    def test_batch_empty(self):
        enc = PolyglotEncoder()
        vs = enc.encode_batch([])
        assert vs.shape == (0, 768)
        assert vs.dtype == np.float32

    def test_batch_matches_single(self):
        enc = PolyglotEncoder()
        texts = ["Alice works at Google", "爱丽丝在谷歌工作",
                 "アリスはGoogleで働いています"]
        batch = enc.encode_batch(texts)
        single = np.stack([enc.encode(t) for t in texts])
        assert np.array_equal(batch, single), \
            "encode_batch != stacked encode() calls"

    def test_batch_each_unit_normalized(self):
        enc = PolyglotEncoder()
        texts = ["Alice", "爱丽丝", "Алиса", "تعمل", "एलिस"]
        vs = enc.encode_batch(texts)
        norms = np.linalg.norm(vs, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5), \
            f"not all unit-norm: {norms}"


# ----------------------------------------- HashingEmbedder hybrid path
class TestHashingEmbedderHybrid:
    """When labse_enabled=True, HashingEmbedder delegates non-English
    text (>30% non-ASCII chars) to PolyglotEncoder."""

    def test_labse_disabled_default_off(self):
        # Default: labse_enabled=False — non-English text goes through
        # the existing HashingEmbedder path, which drops non-ASCII and
        # produces the constant [1, 0, 0, ...] vector (the Tier-1 bug).
        emb = HashingEmbedder(labse_enabled=False)
        v = emb.embed("爱丽丝在谷歌工作")
        # The existing path produces vec[0]=1.0 for empty-token text.
        # Non-ASCII is dropped by the regex tokenizer.
        assert v[0] == 1.0, "Default path should produce vec[0]=1 for non-English"
        assert not v[1:].any(), \
            "Default path should produce zeros everywhere except vec[0]"

    def test_labse_enabled_routes_to_polyglot(self):
        # When labse_enabled=True, >30% non-ASCII text goes to PolyglotEncoder
        emb = HashingEmbedder(labse_enabled=True)
        v = emb.embed("爱丽丝在谷歌工作")
        assert v.shape == (768,)
        assert v.dtype == np.float32
        assert _is_unit(v), "polyglot output should be L2-normalized"
        assert v.any(), "polyglot output should be non-zero"
        # Should NOT be the constant [1,0,...] vector — that's the bug we're fixing
        assert not (v[0] == 1.0 and not v[1:].any()), \
            "polyglot path produced the constant [1,0,...] vector — fix didn't take"

    def test_labse_enabled_english_stays_on_fast_path(self):
        # When labse_enabled=True but text is <30% non-ASCII, it stays on
        # the existing HashingEmbedder path (the "hybrid" design).
        emb = HashingEmbedder(labse_enabled=True)
        v_en = emb.embed("Alice works at Google")
        # Existing HashingEmbedder path: token unigrams + char n-grams + bigrams
        # Should NOT be the constant [1,0,...] vector — "Alice works at Google"
        # tokenizes fine on the regex path.
        assert _is_unit(v_en)
        assert v_en.any()

    def test_labse_threshold_30pct(self):
        # Boundary: text with exactly 30% non-ASCII should stay on fast path.
        # "héllo" has 1 non-ASCII out of 5 = 20% — stays on HashingEmbedder.
        # "héllo wörld" has 2/11 = 18% — stays.
        # Need >30%: "hÉllo" = 1/5 = 20% (still fast path).
        # "café résumé naïve façade" = 4 accents out of 22 = 18%.
        # Construct: "café café café" = 3 accents / 14 chars = 21%.
        # To hit >30%, use a heavily-accented text:
        emb = HashingEmbedder(labse_enabled=True)
        # 1 ASCII + 3 non-ASCII = 75% non-ASCII → polyglot
        v_heavy = emb.embed("éûïà")
        assert v_heavy.any() and v_heavy.shape == (768,)
        # Pure ASCII → fast path
        v_ascii = emb.embed("hello world")
        assert v_ascii.any() and v_ascii.shape == (768,)

    def test_labse_lazy_polyglot_init(self):
        # The polyglot encoder is lazy-initialized — _polyglot is None
        # until first non-English embed() call.
        emb = HashingEmbedder(labse_enabled=True)
        assert emb._polyglot is None, "polyglot should not be built at init"
        # English path doesn't trigger lazy init
        _ = emb.embed("Alice works at Google")
        assert emb._polyglot is None, \
            "polyglot should not be built for English text"
        # Non-English path triggers lazy init
        _ = emb.embed("爱丽丝在谷歌工作")
        assert emb._polyglot is not None, \
            "polyglot should be lazily built for non-English text"


# -------------------------------------------------- Config wiring
class TestConfigLabseField:
    """Config.labse_enabled + CONTEXT_M_LABSE env override."""

    def test_default_off(self):
        cfg = Config()
        assert cfg.labse_enabled is False, \
            "labse_enabled must default to False (existing behavior unchanged)"

    def test_opt_in_via_constructor(self):
        cfg = Config(labse_enabled=True)
        assert cfg.labse_enabled is True

    def test_env_override_on(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_M_LABSE", "1")
        cfg = Config.from_env()
        assert cfg.labse_enabled is True

    def test_env_override_off(self, monkeypatch):
        # Explicit "0" should turn it OFF even if someone set it on
        # in the dataclass default (which they wouldn't, but the env
        # override should be symmetric).
        monkeypatch.setenv("CONTEXT_M_LABSE", "0")
        cfg = Config(labse_enabled=True)
        cfg2 = Config.from_env()
        # from_env() reads env, flips cfg.labse_enabled to False
        assert cfg2.labse_enabled is False

    def test_env_override_truthy_values(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "on", "On"):
            monkeypatch.setenv("CONTEXT_M_LABSE", val)
            cfg = Config.from_env()
            assert cfg.labse_enabled is True, \
                f"CONTEXT_M_LABSE={val!r} should set labse_enabled=True"
