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
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
