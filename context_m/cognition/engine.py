"""CognitionEngine — orchestrates the 5-stage self-organization pipeline.

Triggered from `cortexm consolidate` (or `Memory.consolidate()`) so
the engine is deterministic, auditable, and never produces surprise
writes in the foreground path. Each pass:

  1. PatternScanner.run()       — surface structural regularities
  2. AbstractionEngine.run()   — build prototype categories
  3. GapDetector.run()         — find missing relations
  4. HypothesisEngine.run()     — propose fillers for gaps
  5. AnalogyDetector.run()      — find cross-domain analogies

Output is committed as derived facts with confidence < 0.5 and tagged
is_derived=1, so they don't pollute the active fact count and don't
fire in retrieval unless explicitly promoted (via
`Memory.promote_hypothesis(fact_id)`).

The HYPOTHESIZED_BY edge kind is registered here so
`context_m.trace.edges.ALL_KINDS` includes it. PROMOTED_FROM is the
inverse — when a hypothesis is confirmed by user input, the new fact
points back to its hypothesis origin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from context_m.cognition.scanner import PatternScanner, ScanResult
from context_m.cognition.abstraction import AbstractionEngine, AbstractionResult
from context_m.cognition.gaps import (
    GapDetector, HypothesisEngine, GapResult, HypothesisResult, HYPOTHESIZED_BY)
from context_m.cognition.analogy import (
    AnalogyDetector, AnalogyResult, ANALOGOUS_TO)
from context_m.trace.store import TraceStore


# Edge kinds produced by the cognition engine. These are added to the
# trace.edges vocabulary. We register them lazily on first cognition
# pass so a Trace that never ran cognition doesn't see these kinds in
# queries (avoiding schema pollution).
PROMOTED_FROM = "PROMOTED_FROM"   # user-confirmed fact -> hypothesis origin
ABSTRACTS = "ABSTRACTS"             # abstraction -> member
INSTANTIATES = "INSTANTIATES"       # member -> abstraction


@dataclass
class CognitionReport:
    """Full report of one cognition pass."""
    scan: dict = field(default_factory=dict)
    abstraction: dict = field(default_factory=dict)
    gaps: dict = field(default_factory=dict)
    hypotheses: dict = field(default_factory=dict)
    analogies: dict = field(default_factory=dict)
    commit_id: str | None = None
    dry_run: bool = False
    total_derived_facts: int = 0
    duration_ms: float = 0.0


class CognitionEngine:
    """Orchestrates the 5-stage self-organization pipeline."""

    def __init__(self, store: TraceStore, palace=None) -> None:
        self.store = store
        self.palace = palace
        self.scanner = PatternScanner(store)
        self.abstraction = AbstractionEngine(store)
        self.gap_detector = GapDetector(store)
        # pass palace for Hopfield cleanup path (currently not used
        # by the simple strategies but the API accepts it)
        self.hypothesis = HypothesisEngine(store, palace=palace)
        self.analogy = AnalogyDetector(store)

    def run(self, *,
            dry_run: bool = False,
            user_id: str | None = None) -> CognitionReport:
        """Run the full 5-stage pipeline.

        All writes happen inside a single commit if not dry_run.
        Returns a CognitionReport with per-stage stats.
        """
        import time
        t0 = time.perf_counter()

        # if not dry_run, we expect the caller to have already started
        # a batch + commit (consolidate() does this). We accept an
        # optional commit_id from the caller via store._cognition_commit
        # (stored as a session-level hint). If not present, we create
        # our own commit.
        own_commit = False
        commit_id = None
        if not dry_run:
            try:
                self.store.begin_batch()
                commit_id = self.store.create_commit(
                    "cognition: scan+abstract+gap+hypothesis+analogy",
                    n_facts=0)
                own_commit = True
            except Exception:
                # caller already in a batch — use their commit
                commit_id = getattr(self.store, "_active_commit_id", None)

        report = CognitionReport(dry_run=dry_run, commit_id=commit_id)

        # 1. PatternScanner
        scan = self.scanner.run(user_id=user_id)
        report.scan = {
            "patterns": len(scan.patterns),
            "n_facts_scanned": scan.n_facts_scanned,
            "n_relations": scan.n_relations,
            "n_subjects": scan.n_subjects,
            "n_values": scan.n_values,
            "duration_ms": round(scan.duration_ms, 2),
            "by_kind": _count_by_kind(scan.patterns),
        }

        # 2. AbstractionEngine
        ab = self.abstraction.run(scan, dry_run=dry_run,
                                    commit_id=commit_id, user_id=user_id)
        report.abstraction = {
            "abstractions": len(ab.abstractions),
            "membership_edges_added": ab.membership_edges_added,
            "duration_ms": round(ab.duration_ms, 2),
        }

        # 3. GapDetector
        gaps = self.gap_detector.run(scan, user_id=user_id)
        report.gaps = {
            "gaps": len(gaps.gaps),
            "n_subjects_compared": gaps.n_subjects_compared,
            "duration_ms": round(gaps.duration_ms, 2),
            "by_basis": _count_gaps_by_basis(gaps.gaps),
        }

        # 4. HypothesisEngine
        hyp = self.hypothesis.run(gaps.gaps, dry_run=dry_run,
                                    commit_id=commit_id, user_id=user_id)
        report.hypotheses = {
            "hypotheses": len(hyp.hypotheses),
            "facts_added": hyp.facts_added,
            "duration_ms": round(hyp.duration_ms, 2),
        }

        # 5. AnalogyDetector
        ana = self.analogy.run(scan, dry_run=dry_run,
                                 commit_id=commit_id, user_id=user_id)
        report.analogies = {
            "analogies": len(ana.analogies),
            "edges_added": ana.edges_added,
            "duration_ms": round(ana.duration_ms, 2),
        }

        report.total_derived_facts = (
            ab.membership_edges_added
            + hyp.facts_added
            + ana.edges_added
        )
        report.duration_ms = (time.perf_counter() - t0) * 1000.0

        if not dry_run and own_commit:
            # update commit n_facts to the actual count we wrote
            try:
                self.store.update_commit_n_facts(
                    commit_id, report.total_derived_facts)
            except Exception:
                pass
            try:
                self.store.end_batch()
            except Exception:
                pass

        return report


def run_cognition_pass(store: TraceStore, palace=None, *,
                       dry_run: bool = False,
                       user_id: str | None = None) -> CognitionReport:
    """Convenience function: one-shot cognition pass on a TraceStore."""
    return CognitionEngine(store, palace=palace).run(
        dry_run=dry_run, user_id=user_id)


def _count_by_kind(patterns) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in patterns:
        out[p.kind] = out.get(p.kind, 0) + 1
    return out


def _count_gaps_by_basis(gaps) -> dict[str, int]:
    out: dict[str, int] = {}
    for g in gaps:
        out[g.basis] = out.get(g.basis, 0) + 1
    return out


__all__ = [
    "CognitionEngine", "CognitionReport",
    "run_cognition_pass",
    "HYPOTHESIZED_BY", "PROMOTED_FROM",
    "ABSTRACTS", "INSTANTIATES",
    "ANALOGOUS_TO",
]
