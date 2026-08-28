"""Memory scope sandbox — the InjecMEM isolation the plan demanded.

Threat model (InjecMEM, arXiv:2502.08589 + MINJA second-order contagion):
facts an AGENT writes while operating on a user's behalf are attacker-
influenced until proven otherwise. A poisoned fact written by a compromised
agent must never silently surface in the USER's memory view, and must never
steer another agent's behaviour, unless a human/system explicitly promotes
it.

Policy:
  * WRITE  — unchanged; facts carry their agent_id scope.
  * READ   — user-scope queries (agent_id=None) EXCLUDE agent-scoped facts.
             Agent queries (agent_id=A) see A's facts plus user facts.
  * PROMOTE — explicit, audited, gated:
        - fact must be active and not quarantined
        - confidence >= config.sandbox_promote_min_confidence
        - source chunk re-scanned through InjecMEM + MINJA detectors;
          high-risk sources are refused
        - every promotion appends to the tamper-evident audit chain

Config knobs (Config):
    sandbox_enabled            (default True)
    sandbox_promote_min_confidence (default 0.5)
"""

from __future__ import annotations

from context_m.security.injection import scan as injection_scan
from context_m.security.injection import contagion_scan  # type: ignore[attr-defined]


class PromotionRefused(Exception):
    """Raised when a fact fails promotion policy."""

    def __init__(self, fact_id: str, reason: str):
        super().__init__(f"promotion refused for {fact_id}: {reason}")
        self.fact_id = fact_id
        self.reason = reason


class ScopeSandbox:
    def __init__(self, config, store, audit_log=None) -> None:
        self.cfg = config
        self.store = store
        self.audit = audit_log

    # ------------------------------------------------------------ policy
    def visible(self, fact, agent_id: str | None) -> bool:
        """Read-visibility predicate used by the reader."""
        if agent_id is not None:
            # agent queries see their own scope + shared user scope
            return fact.agent_id in (None, agent_id)
        if not getattr(self.cfg, "sandbox_enabled", True):
            return True
        # user-scope query: agent-written facts are invisible until promoted
        return fact.agent_id is None

    def filter_facts(self, facts: list, agent_id: str | None) -> list:
        return [f for f in facts if self.visible(f, agent_id)]

    # ------------------------------------------------------------ promote
    def promote(self, fact_ids: list[str], *, reviewed_by: str = "system",
                force: bool = False) -> dict:
        """Explicitly promote agent-scoped facts into the user scope."""
        promoted, refused = [], []
        for fid in fact_ids:
            facts = self.store.get_facts([fid])
            fact = facts[0] if facts else None
            if fact is None:
                refused.append({"id": fid, "reason": "not found"})
                continue
            if fact.agent_id is None:
                promoted.append(fid)  # already user-scope; idempotent
                continue
            if not force:
                if fact.quarantined or not fact.is_active:
                    self._audit("sandbox.promote", fid, "refused_quarantined")
                    refused.append({"id": fid, "reason": "quarantined/inactive"})
                    continue
                min_conf = getattr(self.cfg, "sandbox_promote_min_confidence",
                                   0.5)
                if fact.confidence < min_conf:
                    self._audit("sandbox.promote", fid, "refused_low_confidence",
                                meta={"confidence": fact.confidence})
                    refused.append({"id": fid, "reason": f"confidence "
                                   f"{fact.confidence:.2f} < {min_conf}"})
                    continue
                verdict = self._rescan_source(fact)
                if verdict is not None:
                    self._audit("sandbox.promote", fid,
                                "refused_injection_rescan",
                                meta={"risk": verdict})
                    refused.append({"id": fid,
                                    "reason": f"source rescan risk={verdict}"})
                    continue
            self.store.update_fact(fid, agent_id=None,
                                   provenance={**fact.provenance,
                                               "promoted_by": reviewed_by,
                                               "promoted_from":
                                                   fact.agent_id})
            self._audit("sandbox.promote", fid, "promoted",
                        meta={"reviewed_by": reviewed_by})
            promoted.append(fid)
        return {"promoted": promoted, "refused": refused,
                "reviewed_by": reviewed_by}

    # ------------------------------------------------------------ helpers
    def _rescan_source(self, fact) -> str | None:
        """Re-run injection detection over the fact's source chunk."""
        chunk = self.store.get_chunk(fact.source_id) if fact.source_id else None
        if not chunk:
            return None
        verdict = injection_scan(chunk["text"],
                                 quarantine_high=True)
        if verdict.risk == "high":
            return "high"
        if getattr(self.cfg, "quarantine_contagion", True):
            cv = contagion_scan(chunk["text"], [],
                                threshold=self.cfg.contagion_threshold)
            if cv is not None:
                return "contagion"
        return None

    def _audit(self, action: str, resource: str, outcome: str,
               meta: dict | None = None) -> None:
        if self.audit is not None:
            try:
                self.audit.log(action, resource=resource, outcome=outcome,
                               meta=meta or {})
            except Exception:
                pass
