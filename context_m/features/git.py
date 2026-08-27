"""Memory Git — version control for agent memory.

Every add() is a hash-chained commit on a Merkle-style DAG. Branches
fork memory state (A/B-test an agent personality), merges replay
theirs-onto-ours with deterministic conflict resolution via the
lifecycle engine, diffs show exactly which facts changed, and blame
traces which commit introduced a fact. Enterprises get regulatory
rollback and forensic audit as first-class operations.
"""

from __future__ import annotations

import json

from context_m.errors import BranchError
from context_m.trace.fact import Fact, SINGLE_VALUED
from context_m.trace.store import TraceStore
from context_m.util import new_id, similarity


class MemoryGit:
    def __init__(self, store: TraceStore, palace=None) -> None:
        self.store = store
        self.palace = palace

    # ------------------------------------------------------------- basics
    def branch(self, name: str, from_commit: str | None = None,
               switch: bool = True) -> str:
        base = self.store.create_branch(name, from_commit, switch=switch)
        return base

    def checkout(self, name: str) -> None:
        self.store.checkout(name)

    def branches(self) -> list[dict]:
        return self.store.branches()

    def log(self, branch: str | None = None, limit: int = 50) -> list[dict]:
        return self.store.log(branch, limit)

    # ------------------------------------------------------------- sets
    def active_at(self, commit_id: str) -> dict[str, Fact]:
        anc = self.store.ancestry(commit_id)
        rows = self.store.conn.execute(
            "SELECT id, birth_commit, retired_commit FROM facts "
            "WHERE birth_commit IS NOT NULL").fetchall()
        ids = [r["id"] for r in rows
               if r["birth_commit"] in anc
               and (not r["retired_commit"] or r["retired_commit"] not in anc)]
        return {f.id: f for f in self.store.get_facts(ids)}

    def _triple_key(self, f: Fact) -> tuple:
        return (f.subject, f.relation, f.value.lower())

    # ------------------------------------------------------------- diff
    def diff(self, a: str, b: str) -> dict:
        fa, fb = self.active_at(a), self.active_at(b)
        ka = {self._triple_key(f): f for f in fa.values()}
        kb = {self._triple_key(f): f for f in fb.values()}
        added = [kb[k] for k in kb.keys() - ka.keys()]
        removed = [ka[k] for k in ka.keys() - kb.keys()]
        return {
            "from": a, "to": b,
            "added": [{"id": f.id, "fact": f.display(),
                       "valid_from": f.valid_from} for f in added],
            "removed": [{"id": f.id, "fact": f.display(),
                         "valid_from": f.valid_from} for f in removed],
            "n_added": len(added), "n_removed": len(removed),
        }

    # ------------------------------------------------------------- blame
    def blame(self, subject: str, relation: str | None = None,
              user_id: str | None = None) -> list[dict]:
        clauses = ["subject=?"]
        params: list = [subject]
        if relation:
            clauses.append("relation=?")
            params.append(relation)
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        rows = self.store.conn.execute(
            f"SELECT id, relation, value, valid_from, valid_to, is_active, "
            f"birth_commit, tx_from FROM facts WHERE {' AND '.join(clauses)} "
            f"ORDER BY tx_from", params).fetchall()
        out = []
        for r in rows:
            commit = self.store.commit(r["birth_commit"]) if r["birth_commit"] else None
            out.append({
                "fact_id": r["id"],
                "fact": f"({subject}, {r['relation']}, {r['value']})",
                "valid": f"{r['valid_from']}→{r['valid_to'] or '∞'}",
                "active": bool(r["is_active"]),
                "commit": r["birth_commit"],
                "commit_message": commit["message"] if commit else None,
                "recorded_at": r["tx_from"],
            })
        return out

    # ------------------------------------------------------------- merge
    def merge(self, name: str, strategy: str = "latest-wins",
              message: str = "") -> dict:
        """3-way merge of branch ``name`` into the current branch."""
        if strategy not in ("latest-wins", "union"):
            raise BranchError(f"unknown strategy {strategy!r}")
        cur = self.store.current_branch()
        ours_head = self.store.head(cur)
        theirs_head = self.store.head(name)
        if not theirs_head:
            raise BranchError(f"branch {name!r} has no commits")
        if theirs_head == ours_head:
            return {"status": "already-merged", "commit": ours_head,
                    "applied": 0, "conflicts": 0}

        ours = self.active_at(ours_head)
        theirs = self.active_at(theirs_head)
        base_head = self._common_ancestor(ours_head, theirs_head)
        base = self.active_at(base_head) if base_head else {}

        kb = {self._triple_key(f): f for f in base.values()}
        ko = {self._triple_key(f): f for f in ours.values()}
        kt = {self._triple_key(f): f for f in theirs.values()}

        added_by_theirs = [kt[k] for k in kt.keys() - kb.keys()]
        retired_by_theirs = [kb[k] for k in kb.keys() - kt.keys()]

        merge_commit = self.store.create_commit(
            message or f"merge {name} into {cur} ({strategy})",
            branch=cur, parents=[ours_head, theirs_head])
        applied, conflicts = 0, 0

        ours_by_sr: dict[tuple, list[Fact]] = {}
        for f in ours.values():
            ours_by_sr.setdefault((f.subject, f.relation), []).append(f)

        for f in added_by_theirs:
            key = self._triple_key(f)
            if key in ko:
                continue  # already present on our side
            conflict = any(g.value.lower() != f.value.lower()
                           for g in ours_by_sr.get((f.subject, f.relation), [])
                           if g.is_active)
            if conflict and f.relation in SINGLE_VALUED and strategy == "latest-wins":
                for g in ours_by_sr.get((f.subject, f.relation), []):
                    if not g.is_active:
                        continue
                    if (g.tx_from or "") <= (f.tx_from or ""):
                        self.store.update_fact(
                            g.id, is_active=0, retired_commit=merge_commit,
                            tx_to=f.tx_from,
                            provenance={**g.provenance,
                                        "merged_away": merge_commit})
                        conflicts += 1
                self._copy_fact(f, merge_commit)
                applied += 1
            elif conflict:
                self._copy_fact(f, merge_commit)  # union: keep both
                conflicts += 1
                applied += 1
            else:
                self._copy_fact(f, merge_commit)
                applied += 1

        for f in retired_by_theirs:
            key = self._triple_key(f)
            if key in ko and ko[key].is_active:
                self.store.update_fact(
                    ko[key].id, is_active=0, retired_commit=merge_commit,
                    provenance={**ko[key].provenance,
                                "merged_away": f"retired in {name}"})
                applied += 1

        self.store.conn.commit()
        return {"status": "merged", "commit": merge_commit,
                "base": base_head, "applied": applied, "conflicts": conflicts,
                "strategy": strategy}

    def _copy_fact(self, f: Fact, commit_id: str) -> Fact:
        nf = Fact(
            id=new_id(), subject=f.subject, relation=f.relation, value=f.value,
            valid_from=f.valid_from, valid_to=f.valid_to, tx_from=f.tx_from,
            confidence=f.confidence, source_hash=f.source_hash,
            source_id=f.source_id, user_id=f.user_id, agent_id=f.agent_id,
            run_id=f.run_id, memory_type=f.memory_type,
            access_count=f.access_count, reinforcement=f.reinforcement,
            is_active=True, is_derived=f.is_derived,
            provenance={**f.provenance, "merged_from": f.id})
        self.store.insert_fact(nf, commit_id)
        if self.palace is not None:
            self.palace.add(nf.id, self.palace.encode_fact(nf))
        return nf

    def _common_ancestor(self, a: str, b: str) -> str | None:
        anc_a = self.store.ancestry(a)
        anc_b = self.store.ancestry(b)
        common = anc_a & anc_b
        if not common:
            return None
        best, best_ts = None, ""
        for cid in common:
            c = self.store.commit(cid)
            if c and c["ts"] > best_ts:
                best, best_ts = cid, c["ts"]
        return best
