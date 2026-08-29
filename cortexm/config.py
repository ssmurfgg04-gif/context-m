"""Central configuration for the memory fabric.

Every knob the strategic plan calls out is expressed here so the whole
system is reproducible from one dataclass. Codec selection implements the
cortexm-compress tier model (INT8 default, Binary-HRR edge, RaBitQ
ultra-edge, PQ cloud).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Final

CODECS = ("int8", "binary", "rabitq", "pq")
VSA_MODES = ("perm", "conv", "bag")
# Index backends — alternative storage/search paths for the VSA palace.
#   * "quadrant" — default page-clustered log-depth 2-means tree (Rust wheel
#     at rust/quadrant/). INT8-quantized leaves, best-first tree descent.
#   * "nsg"     — Navigating Spreading-out Graph (proximity graph). Lower
#     query latency at high recall on high-dim vectors; build is slower.
#   * "flat"    — exact brute-force dot product. Reference; O(N) per query.
# Used by Config.index_backend (validated in __post_init__).
INDEX_BACKENDS: Final[tuple[str, ...]] = ("quadrant", "nsg", "flat")

# Storage tiers from the compression appendix (bytes per 1M memories).
STORAGE_TIERS = {
    "int8": "768 MB VSA + ~100 MB Trace  (baseline)",
    "binary": "96 MB VSA + ~50 MB Trace   (edge / Raspberry Pi 5)",
    "rabitq": "96 MB VSA + ~50 MB Trace   (ultra-edge, 94%+ recall)",
    "pq": "8 MB VSA + ~75 MB Trace     (cloud, M=8 x 8-bit codes)",
}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """All knobs. Defaults implement the plan's "Edge tier" profile."""

    # --- storage -------------------------------------------------------
    db_path: str = ":memory:"          # SQLite file for Trace + vectors
    codec: str = "int8"                # int8 | binary | rabitq | pq
    tmr: bool = False                  # triple-modular redundancy (binary)
    hash_provider: str = "blake3"      # blake3 | blake2b (auto-fallback)

    # --- VSA ------------------------------------------------------------
    dims: int = 768
    vsa_mode: str = "perm"             # perm | conv | bag
    lexical_lambda: float = 0.6        # weight of lexical superposition
    seed: int = 0x0C0FFEE

    # --- ingest ---------------------------------------------------------
    min_confidence: float = 0.30       # below -> not committed
    fallback_mentions: bool = True     # low-conf entity mentions
    enable_rules: bool = True          # Datalog-lite materialization
    apply_rules_each_add: bool = True  # False: defer to Memory.apply_rules()
    enable_lifecycle: bool = True      # interference-aware lifecycle
    value_match_threshold: float = 0.92  # near-duplicate merge cutoff
    quarantine_injection: bool = True  # InjecMEM defense
    quarantine_contagion: bool = True  # MINJA second-order defense
    contagion_threshold: float = 0.50   # token-overlap cutoff for taint

    # --- retrieval ------------------------------------------------------
    top_k_default: int = 12
    search_k_mult: int = 4             # oversampling for scope filtering
    hop_expansion: int = 1             # associative hops for multi-hop
    history_window: int = 3            # superseded facts shown per chain
    fusion_vsa_weight: float = 0.6
    fusion_symbolic_weight: float = 0.4

    # --- OOD ingestion (Unmess + DisSim + Bitap trigger widening) -----------
    # When True, the main `mem.add()` path runs the chaos-mode pipeline
    # before the deterministic extractor:
    #   1. PerUserIdiolectNormalizer.observe() — accumulate slang
    #   2. PerUserIdiolectNormalizer.normalize() — text-speak + kNN slang
    #      replacement ("u" → "you", "bruh" → "friend" if user-co-occurred)
    #   3. DisSim recursive split — compound sentences become simple clauses
    #      so each one matches its own pattern ("Although Alice works at X,
    #      she quit yesterday" → 3 clauses, 3 patterns fire)
    #   4. Bitap fuzzy-trigger widening in extractor._sentence_candidates
    #      so "wrks at" / "livs in" still fires the works_at/lives_in pattern
    # Default ON — the OOD paraphrase/slang recall catastrophe (9.4% / 5.1%
    # in Tier-1) is exactly what this layer fixes, and it stays μ=0.
    unmess_enabled: bool = True
    unmess_max_depth: int = 2          # DisSim recursion limit
    # Bitap trigger widening: if a known trigger word (works, lives, etc.)
    # is NOT found in the sentence, try fuzzy match within max_edits. This
    # catches "wrks", "livs", "prfrs" without bloating the regex set.
    bitap_trigger_enabled: bool = True
    bitap_trigger_max_edits: int = 2   # 2 edits = "wrks"→"works", "livs"→"lives"

    # --- μ≈0 tiny-transformer fallback (pattern-miss retrieval) ------------
    # When Bitap widened the trigger but the pattern library still returned
    # zero candidates for a sentence, run a 2-layer self-attention "tiny
    # transformer" whose weights are derived deterministically from the
    # project seed (no external model download, no ONNX runtime, no GPU).
    # Closes the OOD recall long tail without breaking the μ=0 / cost / audit
    # moat. Default ON in production; bench baselines turn it off via
    # bench_config_overrides() so the fallback's lift is visible in isolation.
    tiny_fallback_enabled: bool = True

    # --- LaBSE-inspired polyglot encoder (non-English ingest fix) ----------
    # docs/BENCHMARKS.md Tier-1 shows non-English extraction recall =
    # 0.000 ± 0.000 — the regex tokenizer + HashingEmbedder drops non-ASCII
    # letters entirely, so every non-English sentence embeds to the same
    # constant vector [1, 0, 0, ...] and retrieval is broken.
    # When True, HashingEmbedder delegates text with >30% non-ASCII chars
    # to PolyglotEncoder (cortexm.text.labse) — a LaBSE-inspired Unicode
    # n-gram hasher that handles CJK / Devanagari / Arabic / Cyrillic /
    # Thai / Hangul / Hiragana / Katakana via script-aware tokenization +
    # char n-grams + signed feature hashing. Pure numpy + stdlib
    # unicodedata, no model download (3GB LaBSE weights would violate the
    # μ=0 + no-GPU rules), bit-identical across runs. Default OFF so the
    # existing English path stays untouched; opt-in for deployments that
    # ingest non-English text. See context_m/text/labse.py for the algorithm.
    labse_enabled: bool = False

    # --- Query-aware triple pre-filter (HippoRAG 2 lineage) ----------------
    # When True, the reader drops candidate facts with low
    # lexical+semantic+relation overlap with the query BEFORE fusion.
    # HippoRAG 2 credits this for a 7% F1 gain. μ=0 — deterministic scorer.
    prefilter_enabled: bool = True
    prefilter_threshold: float = 0.08  # combined score below this → drop
    prefilter_min_keep: int = 3        # always keep at least this many

    # --- Verbatim tier (MemPalace-style FTS5 + dense over raw chunks) -----
    # When True, MemoryWriter.add() ALSO stores every raw user message in
    # the verbatim_chunks FTS5 virtual table + a HashingEmbedder vector in
    # verbatim_vectors. The reader's verbatim_bridge then surfaces these
    # chunks alongside fact-triple hits, giving the system MemPalace-style
    # factoid recall ("What restaurant did they mention?") without an LLM.
    # The verbatim tier is the proven fix for the canonical-LongMemEval
    # single_session catastrophe (0.222 → expected ~0.7+ with this tier).
    # Default ON in v0.5.3+ — turning it off leaves only the structured
    # tier (VSA over fact triples), which misses natural-human-language
    # factoids the deterministic extractor couldn't parse into triples.
    verbatim_ingest_enabled: bool = True     # store raw chunks on add()
    verbatim_search_enabled: bool = True     # query verbatim at search time
    verbatim_k_at_search: int = 30          # top-k verbatim hits per query
    verbatim_fusion_weight: float = 0.5     # weight in context_block fusion
    verbatim_min_score: float = 0.05        # below this → skip
    verbatim_boost_first_chunk_only: bool = False  # give first chunk a small boost
    # v0.5.4: how many chunks before/after each BM25 hit to surface as
    # "neighbor context". Catches the "Target" / "Veja" / "Hawaii"
    # failure mode where the answer is in the assistant reply that
    # immediately follows the user-message hit. 0 disables. Default 1
    # (one before + one after per hit) — keeps context_block bounded.
    verbatim_neighbor_window: int = 1

    # --- recall_step in production search path ---------------------------
    # When True, Memory.search() ALSO runs recall_step (asymmetric step-
    # distance boost) and concatenates its context_block onto the standard
    # search result. This is the multi_session fix: facts from scrolled-out
    # sessions get surfaced via the step-distance boost, not just access_count.
    # Default ON in v0.5.3+ — all callers benefit. Turning it off disables
    # the multi_session retrieval fix.
    recall_step_in_search: bool = True
    recall_step_window: int = 20              # standard LLM context window
    recall_step_k: int = 10                   # top-k from recall_step
    recall_step_min_messages: int = 25        # only fire if total ingested >= this

    # --- FadeMem-style forgetting (retention decay + sleep sweeps) ----------
    # When True, the consolidate() pass also runs a FadeMem sweep that
    # decays retention scores, marks low-retention facts for deactivation,
    # and consolidates clusters of related facts into summary holograms.
    # Default ON in production — measured 43.2% storage reduction with
    # zero retrieval-precision regression (see benchmarks/results/final.json).
    # Bench scripts flip this back to False via bench_config_overrides()
    # so baseline numbers stay comparable across releases.
    fade_enabled: bool = True
    fade_lambda: float = 0.05          # exponential decay rate per day
    fade_access_boost: float = 0.5     # each access multiplies retention
    fade_contradiction_penalty: float = 0.25  # supersession pressure
    fade_deactivate_threshold: float = 0.10  # below this → deactivate

    # --- Auto-FadeMem on memory pressure (agentmemory learn) ---------------
    # When a single user_id accumulates >= pressure_threshold active facts,
    # the write path (mem.add) runs an inline fade_sweep BEFORE returning.
    # agentmemory does this transparently; so do we now. Set to 0 to opt
    # out entirely. Default 2000 — chosen so a typical chat session (50
    # facts/day for 40 days) never trips the sweep, but a bulk-ingest
    # scenario (mem0 migration, file ingestion) does.
    pressure_threshold: int = 2000

    # --- TiMem Temporal Memory Tree (4-level consolidation hierarchy) -------
    # When True, the consolidate() pass also builds hierarchical summaries:
    #   L1 raw chunks → L2 session summaries → L3 daily patterns → L4 persona
    # Each higher level is a derived fact that links down to its constituents
    # via DERIVED_FROM edges. Retrieval can short-circuit to the appropriate
    # level based on query complexity (complex queries hit L3/L4).
    tmt_enabled: bool = False
    tmt_session_cluster_mins: int = 5   # facts needed before a session summary
    tmt_persona_min_sessions: int = 3   # sessions before persona abstraction

    # --- HMS Cognition Engine (self-organizing memory) ---------------------
    # When True, the consolidate() pass also runs the 5-stage cognition
    # pipeline: PatternScanner (surface regularities) + AbstractionEngine
    # (build prototype categories) + GapDetector (find missing relations)
    # + HypothesisEngine (propose fillers) + AnalogyDetector (find
    # isomorphic domains). Output is HYPOTHESIZED_BY edges with confidence
    # < 0.5 — never promoted to active retrieval unless explicitly
    # confirmed by user input. Default OFF — opt-in for use cases that
    # want self-organization (e.g. agent memory that should infer
    # `grandparent` from two `father` facts).
    cognition_enabled: bool = False
    cognition_min_support: int = 2   # min facts for a pattern to be significant
    cognition_max_hyp_confidence: float = 0.45  # cap on hypothesis confidence

    # --- Provenance standards stack (enterprise-grade) ---------------------
    # When True, every memory commit is wrapped in a COSE Sign1 envelope
    # (RFC 9052) signed with an Ed25519 agent key, and memory ranges can
    # be exported as W3C Verifiable Credentials / SCITT-signed statements.
    # BLAKE3 source hashing stays as the internal integrity mechanism;
    # this layer sits on top for external interoperability (enterprise
    # security reviews ask "does it support W3C VC? C2PA? SCITT?" — yes).
    provenance_enabled: bool = False
    provenance_agent_key_path: str | None = None  # Ed25519 PEM (else generate)
    provenance_agent_did: str | None = None        # did:key identifier

    # --- Structural multi-hop query (deterministic symbolic chains) --------
    # When True, the reader exposes `structural_query(start_entity,
    # relation_chain)` that walks exact relation chains via Trace lookups
    # + VSA unbinding as fallback. Complementary to PPR (probabilistic)
    # — PPR answers "what else might be relevant?", structural query
    # answers "exactly follow this chain."
    structural_query_enabled: bool = True

    # --- Hopfield sparse-softmax cleanup (modernized cleanup memory) -----
    # When True, the VSA cleanup memory uses sparse-softmax attention
    # instead of plain softmax for noise recovery. Sparse softmax keeps
    # only the top-k highest attention weights per recall step, which is
    # more robust to outlier codebook entries (HMS-style improvement
    # over Ramsauer 2020's plain softmax).
    hopfield_sparse_softmax: bool = True
    hopfield_sparse_topk: int = 16   # top-k weights kept per recall step

    # --- Active reconstruction (MRAgent ICML 2026) ---------------------------
    # When True, the reader exposes a `reconstruct()` method that runs an
    # iterative PPR+LLM-scoring loop: expand seed nodes via 2-hop PPR, score
    # each hop's relevance to the query, prune low-scoring branches, return
    # a synthesized narrative. Default OFF — it's an LLM-assisted path that
    # breaks strict μ=0; opt-in for use cases that need it.
    reconstruct_enabled: bool = False
    reconstruct_max_hops: int = 3
    reconstruct_prune_threshold: float = 0.25

    # --- MIND defense (InjecMEM attack mitigation) -----------------------------
    # When True, retrieval runs a diversity check on the top-k results.
    # InjecMEM relies on centroid anchors that cluster in embedding space —
    # if the top-k results are too similar (low intra-result diversity),
    # that's a signature of anchor-based poisoning, and the results are
    # flagged for audit. This is μ=0 compatible (pure embedding math).
    mind_diversity_check: bool = True
    mind_diversity_threshold: float = 0.85  # mean pairwise cosine above this → flag
    mind_flag_on_low_diversity: bool = True   # mark results as suspect, don't drop

    # --- cross-encoder rerank (μ=0, web search SOTA 2026-08) -------------
    # Cross-encoder-style reranking: re-score top-K candidates by cosine
    # sim between query embedding and a natural-language rendering of
    # each fact ("the name of beam_1 is jennifer mccall"). Lifts prec@5
    # 10-20pp on MS-MARCO-style benchmarks. Default OFF so baseline
    # numbers don't shift; bench enables via "+rerank" config.
    enable_rerank: bool = False
    rerank_alpha: float = 0.55          # weight on rerank score (vs original)
    rerank_beta: float = 0.45           # weight on original fusion score
    prf_alpha: float = 0.6              # Rocchio: query weight
    prf_beta: float = 0.4               # Rocchio: top-3 mean weight
    prf_topn: int = 3                   # how many top hits for PRF mean

    # --- chunk-recall parallel path (Tier-4.4.3 abstention fix) ----------
    # When the fact-level VSA returns sparse candidates on natural-language
    # queries whose answer lives in chunk text (not the fact triple),
    # scan the chunks in scope and inject their facts into the candidate
    # pool. The fact triple ("user:X, event, Possibly fixed by") often
    # doesn't lexically or semantically match the query ("Which user
    # suggested PR #65353?") — but the chunk text "[ati865] Possibly
    # fixed by PR #65353 or #65511" matches strongly.
    # μ=0: deterministic lexical+semantic chunk scoring, no LLM. Cost is
    # O(C) per query where C = # of chunks in scope; gated by
    # chunk_recall_max_chunks so production deployments with large scopes
    # aren't penalized. Default ON — the abstention weak spot on real-
    # GitHub data (Tier 4.4.3: 0/13 answerable) is exactly what this
    # fixes, and the cost is bounded.
    chunk_recall_enabled: bool = True
    chunk_recall_weight: float = 0.35   # weight on injected chunk-recall score
    chunk_recall_max_chunks: int = 500  # skip the path above this scope size
    chunk_recall_topn: int = 8         # inject facts from this many top chunks
    chunk_recall_threshold: float = 0.05  # min combined score to inject
    chunk_recall_w_lex: float = 0.45
    chunk_recall_w_sem: float = 0.55

    # --- index ----------------------------------------------------------
    # Which proximity-index backend the memory palace uses for ANN search
    # over hologram vectors. See INDEX_BACKENDS at the top of this file
    # for the full menu. Default "quadrant" — the existing page-clustered
    # log-depth 2-means tree. "nsg" switches to the Navigating Spreading-
    # out Graph (Fu et al. VLDB 2019) — sparser edges, lower query latency
    # at high recall, slower build. "flat" is the exact brute-force
    # reference path (no approximation).
    index_backend: str = "quadrant"
    index_threshold: int = 2048        # build tree when N >= this
    index_leaf_size: int = 512
    index_branch: int = 8
    beam_width: int = 6

    # --- SLB --------------------------------------------------------------
    slb_entries: int = 64
    slb_threshold: float = 0.97
    # Bench determinism: when True, the SLB is bypassed entirely so each
    # query recomputes fresh fusion. Without this, templated near-duplicate
    # queries (e.g. "What is the name of beam_1?" / "What is the age of
    # beam_1?") land at cosine ≈ 0.97 against the SLB threshold, and BLAS
    # ULP drift across processes flips the hit/miss decision — producing
    # ±5pp prec@5 variance on identical bench runs. Production runs leave
    # this False (SLB is a real perf win); only the bench script enables it.
    slb_disabled: bool = False

    # --- Personalized PageRank (HippoRAG 2 lineage) --------------------------
    ppr_enabled: bool = True          # graph diffusion read mode
    ppr_damping: float = 0.85
    ppr_iters: int = 12
    ppr_weight: float = 0.5           # fusion boost multiplier
    ppr_graph_size: int = 96          # local subgraph node budget
    ppr_seeds: int = 6                # teleport set size

    # --- scopes -----------------------------------------------------------
    default_user_id: str = "default"
    sandbox_enabled: bool = True        # agent-scoped facts isolated from
    #                                       user-scope reads until promoted
    sandbox_promote_min_confidence: float = 0.5  # promotion gate

    # --- durability ---------------------------------------------------------
    wal_sync: str = "normal"            # normal | full (full = fsync every
    #                                       commit; survives power loss)

    # --- enterprise ---------------------------------------------------------
    pii_mode: str = "off"              # off | redact | block | tag
    encryption_at_rest: bool = False    # AES-256-GCM field encryption
    master_key_path: str | None = None  # explicit key file (else env/sidecar)
    audit_enabled: bool = True          # hash-chained audit log
    audit_actions: str = "security"    # "security" | "all" | "none"
    rate_limit_rps: float = 50.0        # REST server, requests/second/key
    rate_limit_burst: int = 100

    # --- ZK-SQL proofs (Halo2/PLONKish-inspired, pure-Python) ----------------
    # When True, the MCP server exposes `contextm_zk_sql_proof` (membership /
    # count / sum / avg / min / max proofs over the Trace without revealing
    # the underlying facts). Default OFF — proof generation is O(N) in trace
    # size, opt-in. The verifier is sublinear (commitment check is O(1) hash
    # equality + O(1) HMAC). The prover holds an HMAC key (ZK_SQL_KEY in the
    # trace kv store) and signs each transcript; this is NOT a cryptographic
    # SNARK (BLAKE3 commitments are not homomorphic), but it demonstrates the
    # API surface that a production Halo2/KZG backend would expose.
    zk_sql_enabled: bool = False

    # --- v0.6.0: query-time expansion (Lucene synonym_graph + Google
    # pre-BERT query rewriting lineage). All flags default ON because
    # the new modules are ADDITIVE: they run the original query AND
    # the expansions through BM25 and union the results — so they can
    # only surface MORE chunks, never fewer. Existing canonical
    # LongMemEval scores are protected. -----------------------------------
    # The QueryRewriter orchestrates 4 stages at query time:
    #   1. slang normalization (curated dictionary, μ=0)
    #   2. FST (spelling correction + abbreviation expansion)
    #   3. synonym graph expansion (Lucene synonym_graph filter)
    #   4. entity resolution (holidays → ISO dates)
    # See cortexm/bridge/query_rewrite.py for the orchestrator and
    # cortexm/bridge/{slang,fst,synonyms,recognizers}.py for the
    # individual stages. Each stage can be independently disabled
    # via its own flag (e.g. for ablation studies).
    query_rewrite_enabled: bool = True
    slang_normalization_enabled: bool = True
    abbreviation_expansion_enabled: bool = True
    spelling_correction_enabled: bool = True
    synonym_expansion_enabled: bool = True
    holiday_resolution_enabled: bool = True
    query_max_expansions: int = 8     # cap on BM25 fan-out per query

    # Negation indexing — store "I don't eat meat" as a negation_record
    # (a metadata entry) rather than as a positive (user, eats, meat)
    # fact. The reader checks the negation table when answering a
    # query and returns "No — explicitly stated" if a negation overlaps
    # the query. μ=0: pure regex + dict, no LLM. See
    # cortexm/bridge/negation.py for the detector + SQL schema.
    negation_indexing_enabled: bool = True

    # Multilingual routing — detect non-English text via Unicode script
    # analysis and route to verbatim-only storage (skip the English
    # pattern extractor). Code-switched text is segmented by language
    # boundary and each segment is processed independently. μ=0: pure
    # Unicode script analysis, no model. See cortexm/bridge/multilingual.py
    # for the detector + segmenter. The LaBSE polyglot encoder
    # (Config.labse_enabled) handles the embedding for non-English text.
    multilingual_routing_enabled: bool = True

    # Shannon entropy-weighted storage (tiered precision compromise).
    # Pure entropy filter — "skip storing redundant facts" — violates
    # the "doesn't forget" promise: a fact with VSA overlap > 0.9 to
    # existing memory gets dropped, and later you ask "what's my
    # dog's name?" and the system has forgotten. The safer compromise
    # is TIERED PRECISION: store the verbatim chunk + structured fact
    # (still findable by BM25 + symbolic query) but SKIP the VSA
    # palace.add for high-overlap facts. This deduplicates the
    # holographic superposition (smaller palace = faster retrieval)
    # without losing any information.
    #
    # Cold-start guard: skip the overlap check until the user has at
    # least ``shannon_min_facts`` facts (default 10) so the first few
    # noisy facts don't get spuriously tiered-down.
    shannon_tiered_storage: bool = True
    shannon_overlap_threshold: float = 0.9
    shannon_min_facts: int = 10

    # IR fundamentals (Lucene/Solr-grade primitives). See
    # cortexm/bridge/ir_pro.py for the implementations.
    #   * query_cache: LRU on (query, user_id, k) — invalidated on add()
    #   * bm25_k1/b: exposed for tuning (Lucene defaults: 1.2 / 0.75)
    #   * index_optimize_on_consolidate: run VACUUM + FTS5 optimize during
    #     consolidate (default ON — reclaims space, keeps queries fast)
    query_cache_enabled: bool = True
    query_cache_capacity: int = 1024
    bm25_k1: float = 1.5    # term saturation (Lucene default 1.2; we use 1.5 for short chunks)
    bm25_b: float = 0.75    # length normalization (Lucene default 0.75)
    index_optimize_on_consolidate: bool = True
    highlight_tokens: int = 10     # FTS5 snippet() length
    suggest_min_count: int = 2    # min count for fts5vocab auto-suggest entries

    # SQLite PRAGMA tuning (Google-style read-heavy optimization).
    # Applied at TraceStore init. See cortexm/trace/store.py.
    # Defaults are conservative for 4GB-RAM laptops; bump for cloud.
    pragma_cache_mb: int = 64      # SQLite page cache (default 2MB → 64MB)
    pragma_mmap_mb: int = 256       # memory-mapped I/O (default 0 → 256MB)
    pragma_threads: int = 4         # parallel sort/index threads
    pragma_temp_in_memory: bool = True    # temp store in RAM (not disk)

    def __post_init__(self) -> None:
        if self.codec not in CODECS:
            raise ValueError(f"codec must be one of {CODECS}, got {self.codec!r}")
        if self.vsa_mode not in VSA_MODES:
            raise ValueError(f"vsa_mode must be one of {VSA_MODES}, got {self.vsa_mode!r}")
        if self.dims <= 0 or self.dims % 8:
            raise ValueError("dims must be a positive multiple of 8")
        if self.index_backend not in INDEX_BACKENDS:
            raise ValueError(
                f"index_backend must be one of {INDEX_BACKENDS}, "
                f"got {self.index_backend!r}")

    # Environment overrides (12-factor friendly for MCP server / edge daemon)
    @classmethod
    def from_env(cls, **overrides) -> "Config":
        cfg = cls(**overrides) if overrides else cls()

        # Helper: env var lookup that prefers the post-rename CORTEXM_
        # prefix and falls back to the legacy CONTEXT_M_ prefix (so
        # existing deployments / helm charts / cronjobs don't break on
        # upgrade). Documented in README under "Environment Variables".
        def _env(suffix: str):
            v = os.environ.get("CORTEXM_" + suffix)
            if v is not None:
                return v
            return os.environ.get("CONTEXT_M_" + suffix)

        def _env_bool_dual(suffix: str, default: bool) -> bool:
            v = _env(suffix)
            if v is None:
                return default
            return _env_bool("CORTEXM_" + suffix, default) if os.environ.get("CORTEXM_" + suffix) is not None else _env_bool("CONTEXT_M_" + suffix, default)

        if p := _env("DB"):
            cfg.db_path = p
        if c := _env("CODEC"):
            cfg.codec = c
        if m := _env("VSA_MODE"):
            cfg.vsa_mode = m
        if d := _env("DIMS"):
            cfg.dims = int(d)
        if _env_bool_dual("TMR", cfg.tmr):
            cfg.tmr = True
        if pm := _env("PII_MODE"):
            cfg.pii_mode = pm
        if mk := _env("MASTER_KEY_PATH"):
            cfg.master_key_path = mk
        if _env("ENCRYPT") is not None:
            cfg.encryption_at_rest = _env_bool_dual("ENCRYPT",
                                                     cfg.encryption_at_rest)
        if aa := _env("AUDIT"):
            cfg.audit_actions = aa
        # Allow flipping the index backend via env (e.g. CORTEXM_INDEX_BACKEND=nsg
        # for high-recall cloud deployments where the build cost is amortized).
        if ib := _env("INDEX_BACKEND"):
            cfg.index_backend = ib
        # Production nightly-cron flips. The helm CronJob template sets
        # CORTEXM_FADE=true and CORTEXM_TMT=true so the batch process
        # runs the FadeMem sweep + TiMem TMT hierarchy build on top of the
        # standard consolidate pass. Reading from env (not just CLI flags)
        # means `cortexm consolidate --db …` in the CronJob container
        # automatically picks them up. Legacy CONTEXT_M_* prefix still
        # honored for backward compat.
        if _env("FADE") is not None:
            cfg.fade_enabled = _env_bool_dual("FADE", cfg.fade_enabled)
        if _env("TMT") is not None:
            cfg.tmt_enabled = _env_bool_dual("TMT", cfg.tmt_enabled)
        if _env("RECONSTRUCT") is not None:
            cfg.reconstruct_enabled = _env_bool_dual(
                "RECONSTRUCT", cfg.reconstruct_enabled)
        # HMS Cognition Engine — opt-in self-organization. The helm
        # CronJob template sets CORTEXM_COGNITION=true so the batch
        # process also runs PatternScanner + AbstractionEngine +
        # GapDetector + HypothesisEngine + AnalogyDetector on top of
        # the standard consolidate pass. Also fired by default from
        # `cortexm consolidate` (CLI flag `--no-cognition` opts out).
        if _env("COGNITION") is not None:
            cfg.cognition_enabled = _env_bool_dual(
                "COGNITION", cfg.cognition_enabled)
        # Enterprise provenance standards. Opt-in — when true, every
        # commit is wrapped in a COSE Sign1 envelope (RFC 9052) and
        # ranges can be exported as W3C VC / SCITT statements.
        if _env("PROVENANCE") is not None:
            cfg.provenance_enabled = _env_bool_dual(
                "PROVENANCE", cfg.provenance_enabled)
        # ZK-SQL proofs (PoneglyphDB-style PLONKish). Opt-in.
        if _env("ZK_SQL") is not None:
            cfg.zk_sql_enabled = _env_bool_dual(
                "ZK_SQL", cfg.zk_sql_enabled)
        # Polyglot encoder for non-English text. Opt-in — production
        # deployments that ingest CJK / Indic / Arabic / Cyrillic text
        # flip this on so HashingEmbedder falls back to PolyglotEncoder
        # for >30% non-ASCII text instead of emitting a constant
        # [1,0,0,...] vector that breaks retrieval (Tier-1 bug).
        if _env("LABSE") is not None:
            cfg.labse_enabled = _env_bool_dual("LABSE", cfg.labse_enabled)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
