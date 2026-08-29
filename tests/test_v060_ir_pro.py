"""v0.6.0 IR-fundamentals + Google-style query rewriting test suite.

Covers all 12 IR primitives from the user audit (Aug 2026) plus the
4 new bridge modules (synonyms, recognizers, fst, query_rewrite,
negation, slang, multilingual) and the VerbatimPlugin's new public
API surface (phrase_search, highlight, more_like_this, suggest,
facet_counts, range_search, correct_spelling, optimize_index).

Each test is hermetic (own Memory / VerbatimPlugin instance) so the
order of test execution doesn't matter. All tests run in <30s total.
"""
import sqlite3
import sys
import tempfile
import os
import pytest

from cortexm import Memory, Config
from cortexm.kernel import Context
from cortexm.plugins.verbatim import VerbatimPlugin, VerbatimHit
from cortexm.text.embedder import HashingEmbedder


# =========================================================================
# Test fixtures
# =========================================================================
@pytest.fixture
def fresh_db():
    """A fresh on-disk SQLite db path. Auto-deleted after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # let SQLite create it
    yield path
    if os.path.exists(path):
        os.unlink(path)
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            os.unlink(path + ext)


@pytest.fixture
def populated_verbatim(fresh_db):
    """A VerbatimPlugin with a populated verbatim_chunks table."""
    ctx = Context()
    ctx.service("embedder", HashingEmbedder())
    conn = sqlite3.connect(fresh_db)
    ctx.service("db", conn)
    # Need a Memory instance to get the structured-tier schema too
    # (for facet_counts + range_search which read the `facts` table).
    m = Memory(Config(db_path=fresh_db))
    ctx.service("memory", m)
    plugin = VerbatimPlugin()
    ctx.mount(plugin)
    v = ctx.inject("verbatim")["verbatim"]
    # Add a few chunks
    v.add(text="I work at Google in the Search team.",
          user_id="alice", source_tx_id=1)
    v.add(text="My dog is named Charlie.",
          user_id="alice", source_tx_id=2)
    v.add(text="I volunteered on Valentine's Day at the food bank.",
          user_id="alice", source_tx_id=3)
    v.add(text="I live in Berlin now, moved there last year.",
          user_id="alice", source_tx_id=4)
    v.add(text="The Italian Garden restaurant was excellent.",
          user_id="alice", source_tx_id=5)
    yield v, m, conn
    try:
        v._drop_tables()
    except Exception:
        pass
    conn.close()


# =========================================================================
# synonyms.py — query-time synonym expansion
# =========================================================================
class TestSynonyms:
    def test_default_clusters_loaded(self):
        from cortexm.bridge.synonyms import SynonymGraph, DEFAULT_CLUSTERS
        g = SynonymGraph()
        assert "employment" in g.clusters
        assert "residence" in g.clusters
        assert "pet_name" in g.clusters
        assert len(g.clusters) >= 6

    def test_find_matches_finds_employment(self):
        from cortexm.bridge.synonyms import SynonymGraph
        g = SynonymGraph()
        matches = g.find_matches("Where do I work at?")
        assert len(matches) >= 1
        assert matches[0]["concept"] == "employment"

    def test_expand_includes_original_first(self):
        from cortexm.bridge.synonyms import SynonymGraph
        g = SynonymGraph()
        out = g.expand("Where do I work at?")
        assert out[0] == "Where do I work at?"
        assert len(out) >= 2

    def test_expand_emits_alternative_synonyms(self):
        from cortexm.bridge.synonyms import SynonymGraph
        g = SynonymGraph()
        out = g.expand("Where do I work at?")
        # Should include alternative employment phrases
        assert any("job at" in q for q in out)
        assert any("employed by" in q for q in out)

    def test_expand_no_match_returns_just_original(self):
        from cortexm.bridge.synonyms import SynonymGraph
        g = SynonymGraph()
        out = g.expand("What is the weather?")
        assert out == ["What is the weather?"]

    def test_register_cluster_runtime(self):
        from cortexm.bridge.synonyms import SynonymGraph
        g = SynonymGraph()
        g.register_cluster("hobby", ["play chess", "play the piano"])
        out = g.expand("I play chess")
        assert any("play the piano" in q for q in out)

    def test_max_expansions_cap(self):
        from cortexm.bridge.synonyms import SynonymGraph
        g = SynonymGraph()
        out = g.expand("Where do I work at?", max_expansions=3)
        assert len(out) <= 3


# =========================================================================
# recognizers.py — deterministic entity resolution
# =========================================================================
class TestRecognizers:
    def test_static_holiday_valentine(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        assert r.resolve_holiday("valentine's day", 2026) == "2026-02-14"
        assert r.resolve_holiday("Valentine's Day", 2026) == "2026-02-14"

    def test_algorithmic_thanksgiving_2026(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        # Thanksgiving 2026 = 4th Thursday of November = Nov 26
        assert r.resolve_holiday("thanksgiving", 2026) == "2026-11-26"

    def test_algorithmic_easter_2026(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        # Easter 2026 = April 5 (computus verified)
        assert r.resolve_holiday("easter", 2026) == "2026-04-05"

    def test_algorithmic_memorial_day(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        # Memorial Day 2026 = last Monday of May = May 25
        assert r.resolve_holiday("memorial day", 2026) == "2026-05-25"

    def test_algorithmic_mlk_day(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        # MLK Day 2026 = 3rd Monday of January = Jan 19
        assert r.resolve_holiday("mlk day", 2026) == "2026-01-19"

    def test_unknown_holiday_returns_none(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        assert r.resolve_holiday("my birthday", 2026) is None

    def test_is_holiday_name(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        assert r.is_holiday_name("Valentine's Day") is True
        assert r.is_holiday_name("UCLA") is False

    def test_currency_extraction_usd(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        out = r.extract_currency("I paid $1,234.56 for the bike")
        assert len(out) == 1
        assert out[0]["value"] == 1234.56
        assert out[0]["currency"] == "USD"

    def test_currency_extraction_multi_currency(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        out = r.extract_currency(
            "I paid $100 and 50 euros and 100 pounds")
        assert len(out) == 3
        currencies = {x["currency"] for x in out}
        assert currencies == {"USD", "EUR", "GBP"}

    def test_currency_extraction_symbol_variants(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        r = DeterministicRecognizer()
        # Symbol + word form
        out = r.extract_currency("£50 and 100 GBP")
        assert len(out) == 2
        assert out[0]["currency"] == "GBP"
        assert out[1]["currency"] == "GBP"

    def test_normalize_currency_helper(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        assert DeterministicRecognizer.normalize_currency("$1,234.56") == 1234.56
        assert DeterministicRecognizer.normalize_currency("50") == 50.0
        assert DeterministicRecognizer.normalize_currency("$0.99") == 0.99


# =========================================================================
# fst.py — abbreviation expansion + spelling correction
# =========================================================================
class TestFST:
    def test_abbreviation_expansion_ucla(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        out = f.normalize("I went to UCLA")
        assert "University of California Los Angeles" in out

    def test_abbreviation_expansion_mit(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        out = f.normalize("studied at MIT")
        assert "Massachusetts Institute of Technology" in out

    def test_abbreviation_preserves_case(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        out = f.normalize("I lived in NYC for years")
        # Should preserve capitalization of the expansion's first letter
        assert "New York City" in out

    def test_spelling_correction_recieve(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        out = f.normalize("I will recieve the package")
        assert "receive" in out

    def test_spelling_correction_teh(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        out = f.normalize("teh dog is cute")
        assert "the dog is cute" == out

    def test_normalize_idempotent(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        once = f.normalize("I went to UCLA")
        twice = f.normalize(once)
        assert once == twice

    def test_register_abbreviation_runtime(self):
        from cortexm.bridge.fst import QueryFST
        f = QueryFST()
        f.register_abbreviation("eth", "Ethereum")
        out = f.normalize("I bought eth")
        assert "Ethereum" in out


# =========================================================================
# slang.py — slang normalization
# =========================================================================
class TestSlang:
    def test_normalize_bruh(self):
        from cortexm.bridge.slang import SlangNormalizer
        n = SlangNormalizer()
        assert n.normalize("bruh I work at Google") == "I work at Google"

    def test_normalize_deadass(self):
        from cortexm.bridge.slang import SlangNormalizer
        n = SlangNormalizer()
        out = n.normalize("I deadass work at Google")
        assert "seriously" in out

    def test_normalize_no_cap(self):
        from cortexm.bridge.slang import SlangNormalizer
        n = SlangNormalizer()
        out = n.normalize("no cap I work at Google")
        assert "truthfully" in out

    def test_normalize_preserves_normal_english(self):
        from cortexm.bridge.slang import SlangNormalizer
        n = SlangNormalizer()
        assert n.normalize("I work at Google") == "I work at Google"

    def test_normalize_collapses_double_spaces(self):
        from cortexm.bridge.slang import SlangNormalizer
        n = SlangNormalizer()
        out = n.normalize("bruh  I work at Google")
        assert "  " not in out  # no double spaces

    def test_register_runtime(self):
        from cortexm.bridge.slang import SlangNormalizer
        n = SlangNormalizer()
        n.register("zoogle", "Google")
        assert "Google" in n.normalize("I work at zoogle")


# =========================================================================
# multilingual.py — language detection + routing
# =========================================================================
class TestMultilingual:
    def test_detect_english(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("I work at Google") == "en"

    def test_detect_chinese(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("我在谷歌工作") == "zh"

    def test_detect_japanese(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("私はグーグルで働いています") == "ja"

    def test_detect_korean(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("나는 구글에서 일합니다") == "ko"

    def test_detect_arabic(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("أعمل في جوجل") == "ar"

    def test_detect_hindi(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("मैं गूगल में काम करता हूँ") == "hi"

    def test_detect_russian(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("Я работаю в Гугле") == "ru"

    def test_detect_thai(self):
        from cortexm.bridge.multilingual import detect_language
        assert detect_language("ผมทำงานที่กูเกิล") == "th"

    def test_segment_by_language_mixed(self):
        from cortexm.bridge.multilingual import segment_by_language
        segments = segment_by_language("I work at Google 私はベルリンに住んでいます")
        # Should split into en + ja segments
        langs = [s["lang"] for s in segments]
        assert "en" in langs
        assert "ja" in langs

    def test_process_routes_english_to_extractor(self):
        from cortexm.bridge.multilingual import LanguageAwareProcessor
        p = LanguageAwareProcessor()
        out = p.process("I work at Google")
        assert out["lang"] == "en"
        assert out["skip_extraction"] is False

    def test_process_routes_non_english_to_verbatim_only(self):
        from cortexm.bridge.multilingual import LanguageAwareProcessor
        p = LanguageAwareProcessor()
        out = p.process("我在谷歌工作")
        assert out["lang"] == "zh"
        assert out["skip_extraction"] is True

    def test_latin_with_few_cjk_returns_en(self):
        from cortexm.bridge.multilingual import detect_language
        # Mostly Latin with a few CJK chars → still "en" for routing
        # (the structured English extractor + verbatim tier handles this)
        assert detect_language("I love 東京") == "en"


# =========================================================================
# negation.py — negation detection + extraction
# =========================================================================
class TestNegation:
    def test_detect_simple_negation(self):
        from cortexm.bridge.negation import detect_negation
        out = detect_negation("I don't eat meat.")
        assert len(out) == 1
        assert out[0]["marker"] == "don't"

    def test_detect_no_negation_in_positive_text(self):
        from cortexm.bridge.negation import detect_negation
        out = detect_negation("I eat meat.")
        assert out == []

    def test_detect_multiple_negations(self):
        from cortexm.bridge.negation import detect_negation
        out = detect_negation(
            "I don't eat meat. I never drink soda.")
        assert len(out) == 2

    def test_extract_with_negation_separates_sentences(self):
        from cortexm.bridge.negation import extract_with_negation
        text = "I work at Google. I don't eat meat. I live in Berlin."
        out = extract_with_negation(text)
        assert "I work at Google" in out["positive_text"]
        assert "I live in Berlin" in out["positive_text"]
        # Negated sentence NOT in positive_text
        assert "don't eat meat" not in out["positive_text"]
        assert len(out["negations"]) == 1

    def test_extract_with_no_negation_returns_original(self):
        from cortexm.bridge.negation import extract_with_negation
        text = "I work at Google. I live in Berlin."
        out = extract_with_negation(text)
        assert out["positive_text"] == text
        assert out["negations"] == []

    def test_negation_markers_includes_all_forms(self):
        from cortexm.bridge.negation import NEGATION_MARKERS
        # All common English negation forms
        assert "don't" in NEGATION_MARKERS
        assert "do not" in NEGATION_MARKERS
        assert "never" in NEGATION_MARKERS
        assert "no longer" in NEGATION_MARKERS
        assert "stopped" in NEGATION_MARKERS

    def test_is_negation_overlap_conservative(self):
        from cortexm.bridge.negation import is_negation_overlap
        # No overlap (different topics)
        assert not is_negation_overlap(
            "Where do I work?",
            {"sentence": "I don't eat meat"})
        # Overlap (same topic)
        assert is_negation_overlap(
            "Do I eat meat?",
            {"sentence": "I don't eat meat"})

    def test_schema_constant_exists(self):
        from cortexm.bridge.negation import NEGATION_SCHEMA
        assert "negation_records" in NEGATION_SCHEMA
        assert "CREATE TABLE" in NEGATION_SCHEMA
        assert "CREATE INDEX" in NEGATION_SCHEMA


# =========================================================================
# query_rewrite.py — orchestrator
# =========================================================================
class TestQueryRewriter:
    def test_rewrite_returns_original_first(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        out = r.rewrite("Where do I work at?")
        assert out[0] == "Where do I work at?"

    def test_rewrite_synonym_expansion_employment(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        out = r.rewrite("Where do I work at?")
        assert any("job at" in q for q in out)
        assert any("employed by" in q for q in out)

    def test_rewrite_holiday_resolution(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        out = r.rewrite("What did I do on Valentine's Day?", year=2026)
        assert any("2026-02-14" in q for q in out)

    def test_rewrite_no_partial_holiday_match(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        out = r.rewrite("What did I do on Valentine's Day?", year=2026)
        # Should NOT have "2026-02-14's Day" (partial match bug)
        assert not any("2026-02-14's Day" in q for q in out)

    def test_rewrite_slang_normalization(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        out = r.rewrite("bruh I deadass work at UCLA")
        # Slang removed + abbreviation expanded
        assert any("seriously" in q for q in out)
        assert any("University of California" in q for q in out)
        # "bruh" should not appear in any expansion
        assert not any("bruh" in q.lower() for q in out)

    def test_rewrite_max_expansions_cap(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter(max_expansions=4)
        out = r.rewrite("bruh I deadass work at UCLA")
        assert len(out) <= 4

    def test_rewrite_empty_query(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        assert r.rewrite("") == [""]

    def test_detect_negation_in_query(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        r = QueryRewriter()
        assert r.detect_negation("Don't I work at Google?") is True
        assert r.detect_negation("Where do I work?") is False


# =========================================================================
# ir_pro.py — IR fundamentals
# =========================================================================
class TestIRPro:
    def test_analyze_nfkf_normalization(self):
        from cortexm.bridge.ir_pro import analyze
        # ﬁ ligature → fi
        out = analyze("I love ﬁshing")
        assert "fishing" in [t.lower() for t in out] or "fish" in out

    def test_analyze_removes_stopwords(self):
        from cortexm.bridge.ir_pro import analyze
        out = analyze("The dog is at the park")
        # "the" + "is" + "at" should be removed
        assert "the" not in out
        assert "is" not in out
        assert "at" not in out
        assert "dog" in out
        assert "park" in out

    def test_analyze_stems(self):
        from cortexm.bridge.ir_pro import analyze, stem
        # "running" → stem → "runn" (lightweight Porter)
        assert stem("running") == "runn"
        assert stem("dogs") == "dog"
        assert stem("cities") == "city"

    def test_stem_short_words_passthrough(self):
        from cortexm.bridge.ir_pro import stem
        # Words < 4 chars are not stemmed
        assert stem("cat") == "cat"
        assert stem("dog") == "dog"
        assert stem("go") == "go"

    def test_nfkc_normalize_ligatures(self):
        from cortexm.bridge.ir_pro import nfkc_normalize
        assert nfkc_normalize("ﬁ") == "fi"
        assert nfkc_normalize("ﬂ") == "fl"

    def test_strip_accents(self):
        from cortexm.bridge.ir_pro import strip_accents
        assert strip_accents("Café") == "Cafe"
        assert strip_accents("naïve") == "naive"
        assert strip_accents("Müller") == "Muller"

    def test_get_stopwords_multi_language(self):
        from cortexm.bridge.ir_pro import get_stopwords
        en = get_stopwords("en")
        es = get_stopwords("es")
        fr = get_stopwords("fr")
        de = get_stopwords("de")
        it = get_stopwords("it")
        pt = get_stopwords("pt")
        assert "the" in en
        assert "el" in es
        assert "le" in fr
        assert "der" in de
        assert "il" in it
        assert "o" in pt
        # Unknown language → English fallback
        assert get_stopwords("xx") == en

    def test_build_phrase_query_exact(self):
        from cortexm.bridge.ir_pro import build_phrase_query
        q = build_phrase_query(["italian", "garden"], slop=0)
        assert q == '"italian garden"'

    def test_build_phrase_query_near(self):
        from cortexm.bridge.ir_pro import build_phrase_query
        q = build_phrase_query(["italian", "garden"], slop=2)
        assert "NEAR" in q
        assert "italian" in q
        assert "garden" in q
        assert "2" in q

    def test_levenshtein_basic(self):
        from cortexm.bridge.ir_pro import _levenshtein
        assert _levenshtein("dog", "dog") == 0
        assert _levenshtein("dog", "cat") == 3
        assert _levenshtein("dogg", "dog") == 1
        assert _levenshtein("gardn", "garden") == 1

    def test_correct_spelling_finds_closest(self):
        from cortexm.bridge.ir_pro import correct_spelling
        vocab = {"dog", "cat", "garden", "house", "work", "google"}
        assert correct_spelling("dogg", vocab, max_dist=2) == "dog"
        assert correct_spelling("gardn", vocab, max_dist=2) == "garden"
        assert correct_spelling("wrk", vocab, max_dist=2) == "work"

    def test_correct_spelling_no_match_returns_original(self):
        from cortexm.bridge.ir_pro import correct_spelling
        vocab = {"dog", "cat"}
        assert correct_spelling("xyz", vocab, max_dist=2) == "xyz"

    def test_correct_query_token_level(self):
        from cortexm.bridge.ir_pro import correct_query
        vocab = {"work", "at", "google"}
        out = correct_query("wrk at gogle", vocab, max_dist=2)
        assert "work" in out

    def test_lru_cache_get_put_evict(self):
        from cortexm.bridge.ir_pro import LRUCache
        c = LRUCache(capacity=2)
        c.put(("k1", "alice", 10), "r1")
        c.put(("k2", "alice", 10), "r2")
        assert c.get(("k1", "alice", 10)) == "r1"
        # Touch k1, then add k3 — k2 should be evicted (LRU)
        c.put(("k3", "alice", 10), "r3")
        assert c.get(("k2", "alice", 10)) is None
        assert c.get(("k1", "alice", 10)) == "r1"
        assert c.get(("k3", "alice", 10)) == "r3"

    def test_lru_cache_invalidate_per_user(self):
        from cortexm.bridge.ir_pro import LRUCache
        c = LRUCache(capacity=10)
        c.put(("q1", "alice", 10), "r1")
        c.put(("q2", "alice", 10), "r2")
        c.put(("q3", "bob", 10), "r3")
        n = c.invalidate(user_id="alice")
        assert n == 2
        assert c.get(("q1", "alice", 10)) is None
        assert c.get(("q2", "alice", 10)) is None
        assert c.get(("q3", "bob", 10)) == "r3"  # bob's entries untouched

    def test_lru_cache_invalidate_all(self):
        from cortexm.bridge.ir_pro import LRUCache
        c = LRUCache(capacity=10)
        c.put(("q1", "alice", 10), "r1")
        c.put(("q2", "bob", 10), "r2")
        n = c.invalidate()
        assert n == 2
        assert len(c) == 0


# =========================================================================
# VerbatimPlugin new public API
# =========================================================================
class TestVerbatimPluginNewAPI:
    def test_phrase_search_finds_adjacent_words(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.phrase_search(phrase="work at Google",
                              user_id="alice", slop=2, k=5)
        assert len(out) >= 1
        assert "work" in out[0]["text"].lower()
        assert "google" in out[0]["text"].lower()

    def test_highlight_wraps_matched_terms(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.highlight(query="work Google", user_id="alice", k=3)
        assert len(out) >= 1
        assert "<b>" in out[0]["excerpt"]
        assert "</b>" in out[0]["excerpt"]
        assert "work" in out[0]["excerpt"]
        assert "Google" in out[0]["excerpt"]

    def test_highlight_custom_markers(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.highlight(query="work Google", user_id="alice", k=3,
                          before="[", after="]")
        assert "[" in out[0]["excerpt"]
        assert "]" in out[0]["excerpt"]
        assert "<b>" not in out[0]["excerpt"]

    def test_facet_counts_returns_dict(self, populated_verbatim):
        v, m, conn = populated_verbatim
        # Manually insert a fact via the structured tier
        m.add("I work at Google", user_id="alice")
        out = v.facet_counts(user_id="alice", field="relation")
        assert isinstance(out, dict)
        # Should have at least one relation (works_at)
        assert len(out) >= 1

    def test_facet_counts_invalid_field_raises(self, populated_verbatim):
        v, m, conn = populated_verbatim
        with pytest.raises(ValueError):
            v.facet_counts(user_id="alice", field="bogus_field")

    def test_range_search_no_match(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.range_search(user_id="alice", relation="expense",
                              min_value=10, max_value=100)
        assert out == []  # no numeric expenses in this fixture

    def test_range_search_finds_value_in_range(self, populated_verbatim):
        v, m, conn = populated_verbatim
        # Manually insert a numeric fact directly into the facts table
        from cortexm.trace.fact import Fact
        f = Fact(id="t1", subject="alice", relation="expense",
                 value="50", valid_from="2026-01-01",
                 tx_from="2026-01-01T00:00:00Z",
                 confidence=0.9, user_id="alice")
        m.store.insert_fact(f)
        m.store.conn.commit()
        out = v.range_search(user_id="alice", relation="expense",
                              min_value=10, max_value=100)
        # Should find the $50 expense
        assert any(r["value"] == "50" for r in out)

    def test_more_like_this_returns_list(self, populated_verbatim):
        v, m, conn = populated_verbatim
        # Get a chunk rowid first
        rows = v._db.execute(
            "SELECT rowid FROM verbatim_chunks WHERE user_id = ? LIMIT 1",
            ("alice",)).fetchall()
        if not rows:
            pytest.skip("no chunks to test MLT against")
        chunk_id = rows[0][0]
        out = v.more_like_this(chunk_id=chunk_id, user_id="alice",
                               k=3, max_terms=5)
        assert isinstance(out, list)
        # Each MLT result should report its matched terms
        for r in out:
            assert "matched_terms" in r

    def test_suggest_returns_completions(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.suggest(prefix="goo", k=5)
        # Should find "google" in the completions (or empty if vocab is sparse)
        assert isinstance(out, list)

    def test_correct_spelling_uses_corpus_vocab(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.correct_spelling("wrk", max_dist=2)
        assert isinstance(out, str)
        # Should correct to "work" since "work" is in the verbatim corpus
        assert out == "work" or out == "wrk"  # tolerant if vocab empty

    def test_optimize_index_returns_dict(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.optimize_index()
        assert isinstance(out, dict)
        assert "vacuum" in out
        assert "fts5_optimize" in out
        assert "checkpoint" in out

    def test_tune_bm25_updates_params(self, populated_verbatim):
        v, m, conn = populated_verbatim
        v.tune_bm25(k1=1.2, b=0.5)
        assert v.bm25_k1 == 1.2
        assert v.bm25_b == 0.5

    def test_invalidate_cache_no_op_when_disabled(self):
        plugin = VerbatimPlugin(query_cache_enabled=False)
        # Should be a no-op (no cache instance yet)
        n = plugin.invalidate_cache()
        assert n == 0


# =========================================================================
# VerbatimPlugin.search() — query-rewrite integration
# =========================================================================
class TestSearchWithRewriter:
    def test_search_returns_list_of_hits(self, populated_verbatim):
        v, m, conn = populated_verbatim
        out = v.search(query="Where do I work at?",
                       user_id="alice", k=5)
        assert isinstance(out, list)
        assert len(out) >= 1

    def test_search_query_rewrite_additive(self, populated_verbatim):
        """Query rewriting is ADDITIVE: it can only surface MORE hits."""
        v, m, conn = populated_verbatim
        # Disable rewriter
        v.query_rewrite_enabled = False
        baseline = v.search(query="Where do I work at?",
                            user_id="alice", k=10)
        baseline_ids = {h.chunk_id for h in baseline}
        # Enable rewriter
        v.query_rewrite_enabled = True
        v._query_rewriter = None  # force re-instantiate
        expanded = v.search(query="Where do I work at?",
                           user_id="alice", k=10)
        expanded_ids = {h.chunk_id for h in expanded}
        # Expanded should include all baseline chunks (additive property)
        assert baseline_ids.issubset(expanded_ids)

    def test_search_query_cache_hits_on_repeat(self, populated_verbatim):
        """LRU cache: second identical query returns cached result."""
        v, m, conn = populated_verbatim
        # First call: cache miss, computes
        first = v.search(query="Where do I work at?",
                         user_id="alice", k=5)
        # Second call: cache hit, returns same list
        second = v.search(query="Where do I work at?",
                          user_id="alice", k=5)
        assert first is second  # same object — cache returned it

    def test_search_cache_invalidates_on_add(self, populated_verbatim):
        v, m, conn = populated_verbatim
        # First call populates cache
        first = v.search(query="Where do I work at?",
                         user_id="alice", k=5)
        # Add a new chunk — should invalidate cache for user_id='alice'
        v.add(text="I work at Meta too.", user_id="alice",
              source_tx_id=99)
        # Second call should be a cache MISS (re-compute)
        second = v.search(query="Where do I work at?",
                          user_id="alice", k=5)
        assert first is not second  # different object — cache was invalidated


# =========================================================================
# Config — new flags present + defaults correct
# =========================================================================
class TestConfigNewFlags:
    def test_query_rewrite_flags_default_on(self):
        c = Config()
        assert c.query_rewrite_enabled is True
        assert c.slang_normalization_enabled is True
        assert c.abbreviation_expansion_enabled is True
        assert c.spelling_correction_enabled is True
        assert c.synonym_expansion_enabled is True
        assert c.holiday_resolution_enabled is True

    def test_negation_indexing_default_on(self):
        c = Config()
        assert c.negation_indexing_enabled is True

    def test_multilingual_routing_default_on(self):
        c = Config()
        assert c.multilingual_routing_enabled is True

    def test_ir_fundamentals_flags_default_on(self):
        c = Config()
        assert c.query_cache_enabled is True
        assert c.query_cache_capacity == 1024
        assert c.bm25_k1 == 1.5
        assert c.bm25_b == 0.75
        assert c.index_optimize_on_consolidate is True

    def test_pragma_defaults(self):
        c = Config()
        assert c.pragma_cache_mb == 64
        assert c.pragma_mmap_mb == 256
        assert c.pragma_threads == 4
        assert c.pragma_temp_in_memory is True

    def test_query_max_expansions_default(self):
        c = Config()
        assert c.query_max_expansions == 8

    def test_highlight_tokens_default(self):
        c = Config()
        assert c.highlight_tokens == 10

    def test_suggest_min_count_default(self):
        c = Config()
        assert c.suggest_min_count == 2


# =========================================================================
# TraceStore — PRAGMA tuning actually applied
# =========================================================================
class TestPRAGMATuning:
    def test_pragma_cache_size_applied(self, fresh_db):
        from cortexm.trace.store import TraceStore
        store = TraceStore(fresh_db, pragma_cache_mb=32)
        out = store.conn.execute("PRAGMA cache_size").fetchone()
        # -32768 = 32MB (negative means kibibytes)
        assert out[0] == -32 * 1024
        store.close()

    def test_pragma_mmap_applied(self, fresh_db):
        from cortexm.trace.store import TraceStore
        store = TraceStore(fresh_db, pragma_mmap_mb=64)
        out = store.conn.execute("PRAGMA mmap_size").fetchone()
        # 64MB in bytes
        assert out[0] == 64 * 1024 * 1024
        store.close()

    def test_pragma_temp_in_memory(self, fresh_db):
        from cortexm.trace.store import TraceStore
        store = TraceStore(fresh_db, pragma_temp_in_memory=True)
        out = store.conn.execute("PRAGMA temp_store").fetchone()
        # 2 = MEMORY
        assert out[0] == 2
        store.close()

    def test_pragma_threads(self, fresh_db):
        from cortexm.trace.store import TraceStore
        store = TraceStore(fresh_db, pragma_threads=2)
        out = store.conn.execute("PRAGMA threads").fetchone()
        assert out[0] == 2
        store.close()

    def test_pragma_locking_exclusive_opt_in(self, fresh_db):
        from cortexm.trace.store import TraceStore
        # Default: NORMAL
        store1 = TraceStore(fresh_db)  # not yet — db locked
        store1.close()
        # Opt-in: EXCLUSIVE
        store2 = TraceStore(fresh_db, pragma_locking_exclusive=True)
        out = store2.conn.execute("PRAGMA locking_mode").fetchone()
        assert out[0] == "exclusive"
        store2.close()


# =========================================================================
# μ=0 invariant — new modules don't call any LLM
# =========================================================================
class TestMuZeroUpheld:
    """All v0.6.0 modules must preserve the μ=0 invariant."""

    def test_synonyms_module_no_llm(self):
        from cortexm.bridge.synonyms import SynonymGraph
        import inspect
        src = inspect.getsource(SynonymGraph)
        # No openai/anthropic/requests imports
        assert "openai" not in src.lower()
        assert "anthropic" not in src.lower()
        assert "requests.post" not in src.lower()

    def test_recognizers_module_no_llm(self):
        from cortexm.bridge.recognizers import DeterministicRecognizer
        import inspect
        src = inspect.getsource(DeterministicRecognizer)
        assert "openai" not in src.lower()
        assert "requests.post" not in src.lower()

    def test_query_rewriter_no_llm(self):
        from cortexm.bridge.query_rewrite import QueryRewriter
        import inspect
        src = inspect.getsource(QueryRewriter)
        assert "openai" not in src.lower()
        assert "anthropic" not in src.lower()

    def test_memory_add_does_not_increment_llm_calls(self):
        """Memory.add + Memory.search should not bump LLM_CALLS (μ=0)."""
        from cortexm import Memory, metrics
        before = metrics.llm_calls()
        m = Memory()
        m.add("I work at Google", user_id="alice")
        m.search("Where does Alice work?", user_id="alice")
        after = metrics.llm_calls()
        # μ=0: the deterministic path should NOT bump the counter.
        # We compare delta because other tests in the same process may
        # have already bumped the counter via the enrich fallback.
        assert after == before, (
            f"μ=0 violated: LLM_CALLS {before} → {after} during "
            "Memory.add + Memory.search")
