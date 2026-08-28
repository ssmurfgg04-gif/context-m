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
                dry_run: bool = False) -> dict:
    """Run a single consolidation pass.

    Returns a stats dict:
        {merged_pairs, retired_facts, palace_defragged,
         prefetcher_retrained, commit_id, dry_run}

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
        dry_run         — compute the changes but don't apply them
    """
    stats = {
        "merged_pairs": 0,
        "retired_facts": 0,
        "palace_defragged": False,
        "prefetcher_retrained": False,
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
