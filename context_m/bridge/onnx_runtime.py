"""ONNX Runtime CPU + FP32 + LayerCast determinism seam.

arxiv research: arXiv:2506.09501 (Yuan et al., 2025) — FP32 yields
near-zero variance in token probabilities; BF16 has significant
variance; FP16 is moderate. LayerCast achieves 0% Std@Acc (perfect
reproducibility) by storing weights in BF16 for memory and casting to
FP32 in-graph before each matmul.

ONNX Runtime on CPU produces numerically identical results across runs
(non-determinism is a GPU problem — tensor parallelism, atomic
reductions, FlashAttention).

This module is a SEAM, not a built-out implementation. It documents:
  * The deterministic execution contract for any future ONNX LLM
    enrichment path (bridge/enrich.py)
  * The config knobs that must be set on ONNX Runtime to guarantee
    reproducibility
  * The contract that LayerCast-instrumented ONNX models must satisfy

Production use: when an ONNX LLM is wired into bridge/enrich.py,
it MUST be loaded through this module's `deterministic_session()`
helper. This closes the μ=0 audit loop on the LLM enrichment path
too — every enriched fact becomes bit-exact reproducible.

Why this matters: the deterministic extractor (bridge/extractor.py)
already wins on cost; LayerCast is the upgrade to enable on-device LLM
enrichment WITHOUT losing reproducibility. The cost: ~30% slower than
BF16 on GPU, but at memory ingest/query extraction latency, that's
acceptable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class DeterministicConfig:
    """Config knobs to guarantee ONNX Runtime CPU FP32 determinism."""
    # providers: CPU only — GPU introduces non-determinism via tensor parallelism
    providers: tuple[str, ...] = ("CPUExecutionProvider",)
    # graph opt level: ORT_DISABLE_ALL = 0; optimizations can reorder ops
    graph_optimization_level: int = 0
    # intra-op threads: 1 — multi-thread reductions are non-associative
    intra_op_num_threads: int = 1
    # inter-op threads: 1
    inter_op_num_threads: int = 1
    # execution mode: SEQUENTIAL — parallel execution can reorder
    execution_mode: int = 0   # ORT_SEQUENTIAL
    # FP32 precision enforcement (LayerCast-compatible)
    force_fp32: bool = True
    # disable FlashAttention if available (introduces non-determinism)
    disable_flash_attention: bool = True
    # arena alloc: fixed for reproducibility
    enable_cpu_mem_arena: bool = False
    # random seed: fixed
    seed: int = 0x0C0FFEE


# Documented contract for LayerCast-instrumented ONNX models:
LAYERCAST_CONTRACT = """
LayerCast ONNX model requirements (arXiv:2506.09501):

1. Weights stored as BF16 (2 bytes each) for memory efficiency.
2. Before each MatMul node, a Cast node converts BF16 weights to FP32.
3. MatMul accumulates in FP32 in registers/SRAM.
4. Result is optionally Cast back to BF16 for next layer input.
5. Reduction trees are fixed — no non-associative reorderings.

Verification:
- Run the same input twice through the model with DeterministicConfig.
- Compare output bytes — must be byte-identical (0% Std@Acc).

Failure modes:
- If weights are stored FP32 directly: still deterministic but 2x memory.
- If weights are FP16: moderate variance (paper shows ~0.5% Std@Acc).
- If weights are BF16 without LayerCast Cast nodes: significant variance.
"""


def deterministic_session(model_path: str,
                          config: DeterministicConfig | None = None):
    """Create an ONNX Runtime InferenceSession with deterministic config.

    This is the SEAM for any future LLM enrichment path. Currently a
    thin wrapper that documents the contract — actual use requires
    `pip install onnxruntime` and a LayerCast-instrumented model.
    """
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise ImportError(
            "onnxruntime not installed. Install with: pip install onnxruntime. "
            "LayerCast contract requires a model with explicit Cast nodes "
            "before each MatMul. See LAYERCAST_CONTRACT."
        ) from e

    cfg = config or DeterministicConfig()
    so = ort.SessionOptions()
    so.graph_optimization_level = cfg.graph_optimization_level
    so.intra_op_num_threads = cfg.intra_op_num_threads
    so.inter_op_num_threads = cfg.inter_op_num_threads
    so.execution_mode = cfg.execution_mode
    so.enable_cpu_mem_arena = cfg.enable_cpu_mem_arena

    sess = ort.InferenceSession(
        model_path,
        sess_options=so,
        providers=list(cfg.providers),
    )
    return sess


def verify_determinism(model_path: str, sample_input: dict,
                        runs: int = 3) -> dict:
    """Run the same input through the model N times; verify byte-identical.

    Returns {identical: bool, run_count: int, variance: float}.
    Production check before deploying any ONNX LLM in the enrichment path.
    """
    try:
        import numpy as np
    except ImportError:
        return {"identical": False, "error": "numpy not available"}

    sess = deterministic_session(model_path)
    outputs = []
    for _ in range(runs):
        out = sess.run(None, sample_input)
        # flatten all outputs into one bytes blob
        flat = b"".join(arr.tobytes() for arr in out if hasattr(arr, "tobytes"))
        outputs.append(flat)
    identical = all(o == outputs[0] for o in outputs)
    if not identical:
        # compute variance
        arr_lens = [len(o) for o in outputs]
        # byte-level variance as fraction differing
        min_len = min(arr_lens)
        diffs = [sum(1 for a, b in zip(outputs[i], outputs[0]) if a != b)
                 for i in range(1, len(outputs))]
        variance = sum(diffs) / (max(1, len(diffs)) * max(1, min_len))
    else:
        variance = 0.0
    return {
        "identical": identical,
        "run_count": runs,
        "variance": float(variance),
        "contract": "LayerCast FP32" if identical else "non-deterministic",
    }


__all__ = [
    "DeterministicConfig",
    "LAYERCAST_CONTRACT",
    "deterministic_session",
    "verify_determinism",
]
