"""Bench determinism harness — runtime guard + env pinning.

Problem (root-caused 2026-08):
  Bench runs of "identical" configs produced ±6pp prec@5 swings across
  reruns. Three independent sources of nondeterminism were identified:

  1. PYTHONHASHSEED — Python's process-wide hash randomization
     randomizes set/dict iteration order. The fusion step in
     MemoryReader iterates `candidates` (a dict) and the order of
     insertion affects which fact wins tie-breaks. The pattern library
     also iterates `PATTERNS` (a list — stable) but per-pattern
     `.finditer()` over text — that part is deterministic. The bleed
     is at fusion tie-break and at the symbolic path's set membership
     check for `plan.relations`.

  2. BLAS thread drift — NumPy/SciPy's BLAS backend (OpenBLAS / MKL)
     parallelizes matmuls across threads. The cosine similarity
     between two near-identical templated queries ("What is the name
     of beam_1?" vs "What is the age of beam_1?") lands at ≈ 0.97,
     which is right at the SLB threshold (0.97). BLAS ULP drift across
     threads flips the hit/miss decision and ±0.5pp of prec@5.

  3. SLB cache contamination — the SLB memoizes queries by query-vec
     hash + scope. Templated near-duplicate queries hash to buckets
     that collide, and a cached hit from a sibling query can leak
     a stale result. Bench runs want fresh fusion each time; prod runs
     want the cache. The cfg.slb_disabled flag (set by this harness)
     makes the SLB a no-op for bench mode.

This module exposes:
  * enforce_determinism() — call at the top of any bench script. Checks
    PYTHONHASHSEED, prints a warning, and re-execs the process under
    seed=0 if missing. Also sets OMP/OpenBLAS thread env vars to 1
    (single-threaded BLAS = no ULP drift across threads).
  * bench_config_overrides() — Config kwargs that make a bench run
    bit-for-bit reproducible: slb_disabled=True, plus the seed already
    baked into Config.

Usage:
  from context_m.bench.determinism import enforce_determinism, bench_config_overrides
  enforce_determinism()  # at top of script
  cfg = Config(**bench_config_overrides())
  ...
"""
from __future__ import annotations

import os
import sys


# --- runtime guard --------------------------------------------------------
# Call this AT THE TOP of any bench script, before any other import that
# could touch BLAS / hash tables. It prints a warning if the env is
# non-deterministic and re-execs under seed=0 with BLAS pinned to 1
# thread. The re-exec uses os.execv so the child has the right env from
# the very first bytecode; no warm caches to flush.

def enforce_determinism(*, auto_reexec: bool = True,
                        warn_only: bool = False) -> None:
    """Pin the process to a deterministic mode for bench runs.

    Sets:
      * PYTHONHASHSEED=0 — disables hash randomization
      * OMP_NUM_THREADS=1, OPENBLAS_NUM_THREADS=1, MKL_NUM_THREADS=1 —
        single-threaded BLAS (no ULP drift across threads)
      * OMP_WAIT_POLICY=PASSIVE — no busy-wait contention
      * NUMEXPR_NUM_THREADS=1 — covers numexpr if used

    If PYTHONHASHSEED is not set, this will print a warning and (by
    default) re-exec the script under the corrected env. The re-exec
    uses os.execv so the child has the right env from the very first
    bytecode; no warm caches to flush.

    The re-exec PRESERVES PYTHONPATH (and any other pre-set env vars)
    so scripts that added paths to sys.path at import time continue to
    work after the re-exec.
    """
    needed = {
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OMP_WAIT_POLICY": "PASSIVE",
        "NUMEXPR_NUM_THREADS": "1",
    }
    missing = [k for k, v in needed.items()
               if os.environ.get(k) != v]
    if not missing:
        # all set — process is deterministic, no-op
        return
    msg = ("[determinism] non-deterministic env detected. Missing: "
           + ", ".join(missing)
           + ". Set these for bit-for-bit reproducible bench runs.")
    print(msg, file=sys.stderr, flush=True)
    if warn_only and not auto_reexec:
        return
    if not auto_reexec:
        return
    # re-exec under the corrected env
    env = dict(os.environ)
    env.update(needed)
    # Preserve sys.path additions via PYTHONPATH so post-re-exec imports
    # still resolve. The caller has typically added the project root
    # and scripts/ dir to sys.path; we mirror that into PYTHONPATH.
    extra_paths = [p for p in sys.path if p
                   and p not in ("", os.getcwd())
                   and os.path.isdir(p)]
    if extra_paths:
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (existing_pp + os.pathsep
                              + os.pathsep.join(extra_paths))
    print(f"[determinism] re-execing under deterministic env: {needed}",
          file=sys.stderr, flush=True)
    # execv replaces the current process; the child starts with the
    # correct env from byte 0.
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


# --- Config overrides for bench mode ---------------------------------------
def bench_config_overrides(**extras) -> dict:
    """Config kwargs that make a bench run reproducible.

    Returns a dict you can splat into Config(**...):
      * slb_disabled=True — bypass SLB cache (fresh fusion each query)
      * seed=0x0C0FFEE    — already the default; explicit for clarity
      * enable_rerank=False — default; bench configs opt in via "+rerank"
      * unmess_enabled=False — bench baselines measure the RAW extractor;
        the unmess path is a separate config ("+unmess") so its lift is
        visible in isolation. Production runs leave unmess ON.

    Pass extras to override (e.g. enable_rerank=True for a "+rerank" run).
    """
    overrides = {
        "slb_disabled": True,
        "seed": 0x0C0FFEE,
        "enable_rerank": False,
        "unmess_enabled": False,
        # PPR is deterministic (iters are fixed) but it amplifies any
        # tie-break bleed via the diffusion loop. Bench baselines turn
        # it off; "+ppr" configs turn it on.
        "ppr_enabled": False,
    }
    overrides.update(extras)
    return overrides


__all__ = ["enforce_determinism", "bench_config_overrides"]
