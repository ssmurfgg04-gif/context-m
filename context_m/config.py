"""Central configuration for the memory fabric.

Every knob the strategic plan calls out is expressed here so the whole
system is reproducible from one dataclass. Codec selection implements the
cortexm-compress tier model (INT8 default, Binary-HRR edge, RaBitQ
ultra-edge, PQ cloud).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

CODECS = ("int8", "binary", "rabitq", "pq")
VSA_MODES = ("perm", "conv", "bag")

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

    # --- Query-aware triple pre-filter (HippoRAG 2 lineage) ----------------
    # When True, the reader drops candidate facts with low
    # lexical+semantic+relation overlap with the query BEFORE fusion.
    # HippoRAG 2 credits this for a 7% F1 gain. μ=0 — deterministic scorer.
    prefilter_enabled: bool = True
    prefilter_threshold: float = 0.08  # combined score below this → drop
    prefilter_min_keep: int = 3        # always keep at least this many

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

    # --- TiMem Temporal Memory Tree (4-level consolidation hierarchy) -------
    # When True, the consolidate() pass also builds hierarchical summaries:
    #   L1 raw chunks → L2 session summaries → L3 daily patterns → L4 persona
    # Each higher level is a derived fact that links down to its constituents
    # via DERIVED_FROM edges. Retrieval can short-circuit to the appropriate
    # level based on query complexity (complex queries hit L3/L4).
    tmt_enabled: bool = False
    tmt_session_cluster_mins: int = 5   # facts needed before a session summary
    tmt_persona_min_sessions: int = 3   # sessions before persona abstraction

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

    # --- index ----------------------------------------------------------
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

    def __post_init__(self) -> None:
        if self.codec not in CODECS:
            raise ValueError(f"codec must be one of {CODECS}, got {self.codec!r}")
        if self.vsa_mode not in VSA_MODES:
            raise ValueError(f"vsa_mode must be one of {VSA_MODES}, got {self.vsa_mode!r}")
        if self.dims <= 0 or self.dims % 8:
            raise ValueError("dims must be a positive multiple of 8")

    # Environment overrides (12-factor friendly for MCP server / edge daemon)
    @classmethod
    def from_env(cls, **overrides) -> "Config":
        cfg = cls(**overrides) if overrides else cls()
        if p := os.environ.get("CONTEXT_M_DB"):
            cfg.db_path = p
        if c := os.environ.get("CONTEXT_M_CODEC"):
            cfg.codec = c
        if m := os.environ.get("CONTEXT_M_VSA_MODE"):
            cfg.vsa_mode = m
        if d := os.environ.get("CONTEXT_M_DIMS"):
            cfg.dims = int(d)
        if _env_bool("CONTEXT_M_TMR", cfg.tmr):
            cfg.tmr = True
        if pm := os.environ.get("CONTEXT_M_PII_MODE"):
            cfg.pii_mode = pm
        if mk := os.environ.get("CONTEXT_M_MASTER_KEY_PATH"):
            cfg.master_key_path = mk
        if os.environ.get("CONTEXT_M_ENCRYPT"):
            cfg.encryption_at_rest = _env_bool("CONTEXT_M_ENCRYPT",
                                               cfg.encryption_at_rest)
        if aa := os.environ.get("CONTEXT_M_AUDIT"):
            cfg.audit_actions = aa
        # Production nightly-cron flips. The helm CronJob template sets
        # CONTEXT_M_FADE=true and CONTEXT_M_TMT=true so the batch process
        # runs the FadeMem sweep + TiMem TMT hierarchy build on top of the
        # standard consolidate pass. Reading from env (not just CLI flags)
        # means `cortexm consolidate --db …` in the CronJob container
        # automatically picks them up.
        if os.environ.get("CONTEXT_M_FADE") is not None:
            cfg.fade_enabled = _env_bool("CONTEXT_M_FADE", cfg.fade_enabled)
        if os.environ.get("CONTEXT_M_TMT") is not None:
            cfg.tmt_enabled = _env_bool("CONTEXT_M_TMT", cfg.tmt_enabled)
        if os.environ.get("CONTEXT_M_RECONSTRUCT") is not None:
            cfg.reconstruct_enabled = _env_bool(
                "CONTEXT_M_RECONSTRUCT", cfg.reconstruct_enabled)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
