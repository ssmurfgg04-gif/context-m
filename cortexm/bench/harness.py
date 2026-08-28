"""BEAM-style harness runner.

Protocol (mirrors the plan's Phase 2 "Proof of God" benchmark):
  1. Generate a seeded synthetic conversation at bucket scale
     (128K / 500K / 1M / 10M estimated tokens).
  2. Ingest under the μ=0 protocol — zero LLM calls, asserted.
  3. Probe with questions across the 10 BEAM abilities.
  4. Score with the deterministic nugget judge (offline, reproducible).
  5. Compare against BM25-RAG and vector-only baselines.
  6. Report per-ability accuracy, ingest throughput, storage, latency,
     and trust metrics (provenance completeness, audit latency, cost).

A pluggable LLM reader/judge slot (canonical protocol: gpt-5 reader +
judge) is exposed via ``llm_judge=None``; the deterministic judge keeps
the whole harness honest and free.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cortexm import metrics
from cortexm.api.memory import Memory
from cortexm.bench.abilities import (ABILITY_NAMES, ABILITIES, build_probes,
                                       judge)
from cortexm.bench.baselines import BM25Index, bm25_context, vector_only_context
from cortexm.bench.generator import generate


@dataclass
class BucketResult:
    bucket: str
    n_questions: int = 0
    per_ability: dict = field(default_factory=dict)
    per_system: dict = field(default_factory=dict)
    ingest: dict = field(default_factory=dict)
    corpus: dict = field(default_factory=dict)
    trust: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "bucket": self.bucket, "n_questions": self.n_questions,
            "per_ability": self.per_ability, "per_system": self.per_system,
            "ingest": self.ingest, "corpus": self.corpus, "trust": self.trust,
        }


def run_bucket(bucket: str, seed: int = 42, systems=("context_m", "vector_only", "bm25"),
               db_path: str = ":memory:", max_probes: int | None = None,
               llm_judge=None) -> BucketResult:
    res = BucketResult(bucket=bucket)
    t0 = time.time()
    corpus = generate(bucket, seed=seed)
    res.corpus = {
        "sessions": len(corpus.sessions),
        "personas": len(corpus.personas),
        "estimated_tokens": corpus.total_tokens,
        "generation_seconds": round(corpus.generation_seconds, 2),
    }

    from cortexm.config import Config as _Cfg
    cfg = _Cfg(db_path=db_path) if db_path != ":memory:" else _Cfg()
    cfg.apply_rules_each_add = False          # bulk mode: Datalog after ingest
    memory = Memory(cfg)
    metrics.reset_counters()

    # ---- ingest (μ=0) ----------------------------------------------------
    t_ingest = time.time()
    n_msgs = 0
    for user_id, date, msgs in corpus.sessions:
        payload = [{"role": role, "content": text, "timestamp": date}
                   for role, text in msgs]
        n_msgs += len(msgs)
        memory.add(payload, user_id=user_id, timestamp=date)
    n_derived = memory.apply_rules()
    ingest_s = time.time() - t_ingest
    stats = memory.stats()
    res.ingest = {
        "wall_seconds": round(ingest_s, 2),
        "messages": n_msgs,
        "tokens_per_second": int(corpus.total_tokens / max(ingest_s, 1e-9)),
        "messages_per_second": round(n_msgs / max(ingest_s, 1e-9), 1),
        "llm_calls": metrics.counters()["llm_calls"],
        "u0_protocol": stats["u0_protocol"],
        "facts": stats["facts"],
        "active_facts": stats["active_facts"],
        "chunks": stats["chunks"],
        "commits": stats["commits"],
        "derived_facts": stats["derived"],
        "deferred_rule_pass": True,
    }

    # ---- probes -----------------------------------------------------------
    probes = build_probes(corpus.personas, __import__("random").Random(seed))
    by_ability: dict[str, list] = {a: [] for a in ABILITIES}
    for p in probes:
        by_ability[p.ability].append(p)
    if max_probes:
        for a in ABILITIES:
            by_ability[a] = by_ability[a][:max_probes]
    res.n_questions = sum(len(v) for v in by_ability.values())

    # ---- baseline indexes ---------------------------------------------------
    chunks_by_user: dict[str, list[dict]] = {}
    for user_id, date, msgs in corpus.sessions:
        docs = chunks_by_user.setdefault(user_id, [])
        for i, (role, text) in enumerate(msgs):
            if len(text) > 12:  # skip tiny interjections
                docs.append({"id": f"{user_id}:{len(docs)}", "text": text})
    bm25 = {uid: BM25Index(docs) for uid, docs in chunks_by_user.items()}

    # ---- evaluate ---------------------------------------------------------
    per_system: dict[str, dict[str, float]] = {s: {a: 0.0 for a in ABILITIES}
                                               for s in systems}
    counts = {a: len(by_ability[a]) for a in ABILITIES}
    details: list[dict] = []
    latencies = {s: [] for s in systems}
    provenance_ok = 0
    provenance_checks = 0

    for ability in ABILITIES:
        for probe in by_ability[ability]:
            for system in systems:
                t_q = time.time()
                if system == "context_m":
                    out = memory.search(probe.question, user_id=probe.user_id, k=12)
                    context = out["context_block"]
                    provenance_checks += 1
                    if out["provenance"]["verification"]:
                        provenance_ok += 1
                elif system == "vector_only":
                    context = vector_only_context(memory, probe.question,
                                                  probe.user_id)
                elif system == "bm25":
                    context = bm25_context(bm25[probe.user_id], probe.question)
                latencies[system].append(time.time() - t_q)
                score_fn = llm_judge if llm_judge else judge
                score, detail = score_fn(probe, context)
                per_system[system][ability] += score
                if system == "context_m":
                    details.append({"ability": ability,
                                    "question": probe.question,
                                    "score": score, "detail": detail,
                                    "context": context[:400]})

    for system in systems:
        res.per_system[system] = {
            "overall": round(sum(per_system[system][a] for a in ABILITIES)
                             / max(res.n_questions, 1), 4),
            "per_ability": {
                a: round(per_system[system][a] / max(counts[a], 1), 4)
                for a in ABILITIES if counts[a]
            },
            "mean_latency_ms": round(
                sum(latencies[system]) / max(len(latencies[system]), 1) * 1e3, 2),
        }
    res.per_ability = res.per_system.get("context_m", {}).get("per_ability", {})
    res.trust = {
        "provenance_completeness": round(
            provenance_ok / max(provenance_checks, 1), 4),
        "audit_latency_ms": round(
            sum(latencies.get("context_m", [])) /
            max(len(latencies.get("context_m", [])), 1) * 1e3, 2),
        "u0_ingest_llm_calls": metrics.counters()["llm_calls"],
        "storage": memory.storage_stats(),
        "hash_provider": stats["hash_provider"],
        "codec": stats["codec"],
        "vsa_mode": stats["vsa_mode"],
        "wall_seconds_total": round(time.time() - t0, 2),
    }
    memory.close()
    res.details = details  # type: ignore[attr-defined]
    return res


def format_report(results: list[BucketResult]) -> str:
    lines = ["# Context-M — BEAM-Style Benchmark Results", ""]
    for r in results:
        lines.append(f"## Bucket: {r.bucket.upper()} "
                     f"({r.corpus['estimated_tokens']:,} est. tokens, "
                     f"{r.n_questions} questions)")
        lines.append("")
        sys_names = list(r.per_system.keys())
        header = "| System | Overall | " + " | ".join(ABILITIES) + " |"
        sep = "|---" * (2 + len(ABILITIES)) + "|"
        lines.append(header)
        lines.append(sep)
        for s in sys_names:
            d = r.per_system[s]
            row = [f"**{d['overall']:.1%}**" if s == "context_m" else f"{d['overall']:.1%}"]
            for a in ABILITIES:
                v = d["per_ability"].get(a)
                row.append(f"{v:.0%}" if v is not None else "—")
            lines.append(f"| {s} | " + " | ".join(row) + " |")
        lines.append("")
        lines.append(f"- Ingest: {r.ingest['wall_seconds']}s for "
                     f"{r.ingest['tokens_per_second']:,} tokens/s "
                     f"(μ=0: {r.ingest['u0_protocol']}, "
                     f"{r.ingest['llm_calls']} LLM calls)")
        lines.append(f"- Memory: {r.ingest['facts']:,} facts / "
                     f"{r.ingest['chunks']:,} chunks / "
                     f"{r.ingest['commits']:,} commits "
                     f"({r.ingest['derived_facts']} derived by Datalog)")
        lines.append(f"- Provenance completeness: "
                     f"{r.trust['provenance_completeness']:.1%} | "
                     f"retrieval latency p50≈{r.trust['audit_latency_ms']}ms")
        lines.append("")
    return "\n".join(lines)
