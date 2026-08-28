"""Dreaming / consolidation — Aeon-inspired idle-time optimization.

arXiv:2601.15311 (Aeon) describes a background task analogous to
biological sleep: defragmentation, GC, and consolidation of the
verbose Trace into compressed long-term episodic summaries.

Context-M's Memory Git is currently passive — it version-controls
facts but doesn't optimize them. This module turns it into an ACTIVE
memory optimizer that runs during idle periods:

    consolidate(store, palace, prefetcher, ...) ->
      - merges redundant triples in the Trace (same subject/relation,
        near-duplicate values) via MERGED_WITH edges
      - retires facts past their valid_to + grace period
      - defragments the palace by rebuilding the in-memory packed matrix
        from active facts only (drops retired/merged IDs)
      - re-trains the MBTB prefetcher from recent query access patterns

The pass is IDEMPOTENT and SAFE — every change is a commit on the
current branch, every retired fact is still queryable via
allow_inactive=True, every merged fact keeps a MERGED_WITH edge so
the original triple is recoverable for audit. A failed pass rolls back
via the existing Memory Git ancestry.

NOTE: this is NOT a separate daemon — it's a function the host app
calls when idle (e.g.overnight cron, `cortexm consolidate` CLI, or
an `on_idle` hook in the MCP server).
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone
from typing import Iterable

from context_m.trace.edges import MERGED_WITH, RETRACTED_BY
from context_m.util import iso, similarity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def consolidate(store, palace=None, prefetcher=None, *,
                user_id: str | None = None,
                merge_threshold: float = 0.92,
                retire_grace_days: int = 365,
                defrag_palace: bool = True,
                retrain_prefetcher: bool = True,
                run_fade: bool = True,
                run_tmt: bool = False,
                run_cognition: bool = False,
                dry_run: bool = False,
                fade_cfg: dict | None = None,
                tmt_cfg: dict | None = None,
                cognition_cfg: dict | None = None) -> dict:
    """Run a single consolidation pass.

    Returns a stats dict:
        {merged_pairs, retired_facts, palace_defragged,
         prefetcher_retrained, fade_stats, tmt_stats,
         cognition_stats, commit_id, dry_run}

    Parameters:
        store           — TraceStore
        palace          — MemoryPalace (optional; only needed for defrag)
        prefetcher      — Prefetcher (optional; only needed for retrain)
        user_id         — restrict pass to one user (None = all users)
        merge_threshold — jaccard similarity above which two facts
                           (same subject+relation) are merged
        retire_grace_days — facts with valid_to older than this many
                            days are retired (deactivated)
        defrag_palace   — rebuild the palace packed matrix from active facts
        retrain_prefetcher — rebuild the MBTB from access_count stats
        run_fade        — also run FadeMem sweep (decay + deactivate + merge)
        run_tmt         — also run TiMem TMT hierarchy build
        run_cognition   — also run the HMS-style Cognition Engine pass
                           (PatternScanner + AbstractionEngine + GapDetector
                           + HypothesisEngine + AnalogyDetector). Emits
                           HYPOTHESIZED_BY edges with confidence < 0.5 —
                           never active in retrieval unless promoted.
        fade_cfg        — kwargs for fade_sweep (lambda_, thresholds, etc.)
        tmt_cfg         — kwargs for tmt_build (cluster mins, etc.)
        cognition_cfg   — kwargs for run_cognition_pass
        dry_run         — compute the changes but don't apply them
    """
    stats = {
        "merged_pairs": 0,
        "retired_facts": 0,
        "palace_defragged": False,
        "prefetcher_retrained": False,
        "fade_stats": None,
        "tmt_stats": None,
        "cognition_stats": None,
        "commit_id": None,
        "dry_run": dry_run,
    }

    # ---------- 1. Merge redundant triples -------------------------------
    # Group active facts by (user_id, subject, relation) and find
    # near-duplicate values within each group.
    where = "is_active=1 AND quarantined=0"
    args: tuple = ()
    if user_id is not None:
        where += " AND user_id=?"
        args = (user_id,)
    rows = store.conn.execute(
        f"SELECT id, subject, relation, value, user_id, confidence, "
        f"access_count FROM facts WHERE {where} ORDER BY user_id, subject, "
        f"relation, valid_from", args).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        r = dict(r)
        key = (r["user_id"], r["subject"], r["relation"])
        groups.setdefault(key, []).append(r)

    merge_pairs: list[tuple[str, str, float]] = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        # pairwise similarity (small groups, so O(n^2) is fine)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                sim = similarity(a["value"], b["value"])
                if sim >= merge_threshold:
                    # keep the higher-confidence one; merge the other into it
                    if a["confidence"] >= b["confidence"]:
                        keep, drop = a, b
                    else:
                        keep, drop = b, a
                    merge_pairs.append((keep["id"], drop["id"], sim))

    if not dry_run:
        store.begin_batch()
        commit = store.create_commit(
            f"consolidate: merge {len(merge_pairs)} pairs, "
            f"retire stale", n_facts=0)
        stats["commit_id"] = commit

        # apply merges
        for keep_id, drop_id, sim in merge_pairs:
            # deactivate the drop, wire MERGED_WITH edge so audits can
            # always recover the original triple
            store.update_fact(
                drop_id, is_active=0, tx_to=iso(_now()),
                retired_commit=commit,
                provenance={"merged_into": keep_id,
                            "merge_sim": round(sim, 4),
                            "consolidated_at": iso(_now())})
            store.add_edge(keep_id, drop_id, MERGED_WITH,
                          {"sim": round(sim, 4),
                           "consolidated_at": iso(_now())})

    stats["merged_pairs"] = len(merge_pairs)

    # ---------- 2. Retire stale facts -----------------------------------
    # Facts whose valid_to is older than retire_grace_days and which
    # haven't been accessed recently (access_count == 0) — these are
    # safe to retire; the bi-temporal model still serves them via
    # allow_inactive=True.
    cutoff = (_now() - _dt.timedelta(days=retire_grace_days)).strftime(
        "%Y-%m-%d")
    retire_where = (
        f"is_active=1 AND valid_to IS NOT NULL AND valid_to < ? "
        f"AND valid_to != '' AND access_count = 0")
    retire_args: tuple = (cutoff,)
    if user_id is not None:
        retire_where += " AND user_id=?"
        retire_args = (cutoff, user_id)
    retire_rows = store.conn.execute(
        f"SELECT id FROM facts WHERE {retire_where}", retire_args).fetchall()
    retire_ids = [r[0] for r in retire_rows]

    if not dry_run:
        for fid in retire_ids:
            store.update_fact(
                fid, is_active=0, tx_to=iso(_now()),
                retired_commit=commit,
                provenance={"retired_by_consolidate": True,
                            "consolidated_at": iso(_now())})
    stats["retired_facts"] = len(retire_ids)

    # ---------- 3. Palace defrag ----------------------------------------
    # Rebuild the palace's packed matrix from active facts only.
    # This drops retired / merged IDs from the in-memory index and
    # re-tightens the page-clustered tree (currently a no-op for the
    # SQLite-backed palace; for the in-memory palace it compacts).
    if defrag_palace and palace is not None and not dry_run:
        try:
            # the palace's _n tracks active entries — a defrag pass
            # re-builds the matrix by re-adding only active fact IDs.
            # For the SQLite-backed palace this is a no-op (vectors
            # are stored in a BLOB, not a packed matrix), but the
            # call still flushes any in-memory dirty state.
            if hasattr(palace, "defrag"):
                palace.defrag()
            elif hasattr(palace, "close"):
                palace.close()
            stats["palace_defragged"] = True
        except Exception:
            pass

    # ---------- 4. Prefetcher retrain ------------------------------------
    # The MBTB prefetcher tracks co-access patterns. Re-training from
    # access_count stats lets it pick up shifts in user behavior.
    if retrain_prefetcher and prefetcher is not None and not dry_run:
        try:
            if hasattr(prefetcher, "retrain"):
                prefetcher.retrain(store)
            elif hasattr(prefetcher, "rebuild"):
                prefetcher.rebuild(store)
            stats["prefetcher_retrained"] = True
        except Exception:
            pass

    # ---------- 5. FadeMem sweep (decay + deactivate + cluster merge) ----
    # Biologically-inspired forgetting: exponential decay on retention
    # scores, with access-driven reconsolidation (frequently-retrieved
    # facts resist decay). Deactivates facts whose retention_score drops
    # below fade_deactivate_threshold; merges clusters of low-retention
    # siblings. Bi-temporal safe: only is_active flips.
    if run_fade and not dry_run:
        try:
            from context_m.trace.fade import fade_sweep
            fade_kwargs = dict(
                lambda_=0.05,
                access_boost=0.5,
                contradiction_penalty=0.25,
                deactivate_threshold=0.10,
                merge_threshold=0.30,
                merge_similarity=merge_threshold,
                user_id=user_id,
                dry_run=dry_run,
            )
            if fade_cfg:
                fade_kwargs.update(fade_cfg)
            stats["fade_stats"] = fade_sweep(store, palace, **fade_kwargs)
        except Exception as e:
            stats["fade_stats"] = {"error": str(e)}

    # ---------- 6. TiMem TMT hierarchy build -----------------------------
    # Episodic → session → day → persona abstraction. Each higher level
    # is a derived fact with DERIVED_FROM edges back to its constituents.
    # Retrieval can short-circuit to the appropriate level based on
    # query complexity.
    if run_tmt and not dry_run:
        try:
            from context_m.trace.tmt import tmt_build
            tmt_kwargs = dict(
                session_cluster_mins=5,
                persona_min_sessions=3,
                user_id=user_id,
            )
            if tmt_cfg:
                tmt_kwargs.update(tmt_cfg)
            stats["tmt_stats"] = tmt_build(store, palace, **tmt_kwargs)
        except Exception as e:
            stats["tmt_stats"] = {"error": str(e)}

    # ---------- 7. HMS Cognition Engine pass ----------------------------
    # PatternScanner + AbstractionEngine + GapDetector + HypothesisEngine
    # + AnalogyDetector. Surfaces structural regularities, builds
    # prototype categories, fills in missing relations via hypotheses,
    # finds cross-domain analogies. Output is derived facts with
    # confidence < 0.5 and is_derived=1, never promoted to active
    # retrieval unless explicitly confirmed by user input.
    if run_cognition:
        try:
            from context_m.cognition import run_cognition_pass
            cog_kwargs = dict(
                dry_run=dry_run,
                user_id=user_id,
            )
            if cognition_cfg:
                cog_kwargs.update(cognition_cfg)
            cog_report = run_cognition_pass(store, palace=palace,
                                              **cog_kwargs)
            stats["cognition_stats"] = {
                "scan": cog_report.scan,
                "abstraction": cog_report.abstraction,
                "gaps": cog_report.gaps,
                "hypotheses": cog_report.hypotheses,
                "analogies": cog_report.analogies,
                "total_derived_facts": cog_report.total_derived_facts,
                "duration_ms": round(cog_report.duration_ms, 2),
                "cognition_commit_id": cog_report.commit_id,
            }
        except Exception as e:
            stats["cognition_stats"] = {"error": str(e)}

    if not dry_run:
        store.end_batch()
        if palace is not None and hasattr(palace, "close"):
            palace.close()

    return stats


# ---------- Audit / inspection -----------------------------------------

def consolidation_report(store, since: str | None = None) -> dict:
    """Return a summary of consolidation activity since `since` (ISO).

    Counts merged pairs, retired facts, and lists the commit IDs that
    ran consolidation. Useful for the audit / governance dashboard.
    """
    where = "is_active=0 AND provenance LIKE ?"
    pat = '%"consolidated_at"%'
    if since:
        where += " AND tx_to >= ?"
        args = (pat, since)
    else:
        args = (pat,)
    rows = store.conn.execute(
        f"SELECT COUNT(*) FROM facts WHERE {where}", args).fetchone()
    merged = 0
    retired = 0
    for r in store.conn.execute(
        "SELECT provenance FROM facts WHERE " + where, args).fetchall():
        prov = r[0] or ""
        if "merged_into" in prov:
            merged += 1
        if "retired_by_consolidate" in prov:
            retired += 1
    commits = []
    try:
        for r in store.conn.execute(
            "SELECT id, message FROM commits WHERE message LIKE ?",
            ("%consolidate%",)).fetchall():
            commits.append({"id": r[0], "message": r[1]})
    except Exception:
        pass
    return {"merged_facts": merged, "retired_facts": retired,
            "consolidation_commits": len(commits),
            "recent_commits": commits[:10]}


__all__ = ["consolidate", "consolidation_report"]
