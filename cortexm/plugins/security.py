"""Security middleware plugin — MINJA + MIND + PermissionGate as
compose-anywhere middleware.

Reddit deep-dive (2026-08-29): security was the #2 most-mentioned
pain point (46 mentions of "provenance" + 18 of "hallucinates" /
"misses"). Cordis' composability guarantee says: security should
be a plugin, not baked in. Users who want it mount it; users who
don't, skip it.

User directive (2026-08-29): "no malicious code shall be executed
to read user data without explicit permission but I think yes we
can just add that to the security plugins."

This plugin wraps three existing defenses as kernel middleware:

  1. MINJA (``cortexm.security.injection``) — pattern scan at
     INGEST time. High-risk patterns (ignore instructions, jailbreak,
     exfiltration) quarantine the offending fact; medium-risk patterns
     flag provenance for audit.

  2. MIND (``cortexm.security.mind``) — diversity check at RETRIEVAL
     time. If the top-k facts are too similar (low diversity), the
     retrieval is flagged as a possible InjecMEM anchor-based poisoning.

  3. PermissionGate (``cortexm.security.permission``) — default-deny
     gate for code-execution + user-data reads. Plugins that want to
     invoke ``os.system`` / ``subprocess`` / ``open()`` MUST first
     call ``permission.grant_read(path)`` / ``permission.grant_exec(cmd)``
     — otherwise the gate denies and audits the attempt.

All three are μ=0 (no LLM, no API call).

Usage::

    from cortexm.kernel import Context
    from cortexm.plugins.security import SecurityPlugin
    from cortexm.api.memory import Memory

    ctx = Context()
    mem = Memory()
    ctx.service("memory", mem)
    ctx.mount(SecurityPlugin())

    sec = ctx.inject("security")["security"]
    verdict = sec.scan_ingest("ignore previous instructions and ...")
    # verdict.risk == "high"  → quarantine
    # verdict.risk == "medium" → audit flag
    # verdict.risk == "none"   → safe to ingest

    hits = mem.search("where did I eat?", user_id="alice")
    mind_verdict = sec.scan_retrieval(hits)
    # mind_verdict.diversity ∈ [0,1]; .flagged = True if too low

    # An agentic tool plugin wants to run "ls /tmp/agent_ws"
    perm = sec.permission
    perm.grant_read("/tmp/agent_ws")        # explicit user grant
    perm.grant_exec("ls")                    # explicit exec grant
    v = perm.can_exec("ls /tmp/agent_ws")   # .allowed = True
    v = perm.can_read("/etc/passwd")        # .allowed = False (sensitive)
    v = perm.can_exec("curl evil.com")       # .allowed = False (sensitive)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cortexm.security import injection as minja
from cortexm.security import mind
from cortexm.security import permission as _permission_mod
from cortexm.security.permission import PermissionGate


@dataclass
class SecurityVerdict:
    """Combined verdict from MINJA + MIND checks."""
    ingest_risk: str = "none"            # none|medium|high
    ingest_quarantined: bool = False
    ingest_rules: list[str] = None
    retrieval_diversity: float = 0.0
    retrieval_flagged: bool = False
    retrieval_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ingest_risk": self.ingest_risk,
            "ingest_quarantined": self.ingest_quarantined,
            "ingest_rules": self.ingest_rules or [],
            "retrieval_diversity": round(self.retrieval_diversity, 4),
            "retrieval_flagged": self.retrieval_flagged,
            "retrieval_reason": self.retrieval_reason,
        }


class SecurityPlugin:
    """Mounts MINJA + MIND + PermissionGate as kernel services.

    This plugin does NOT enforce policy — it returns verdicts. The
    caller (or the kernel's pipeline middleware, if mounted)
    decides whether to quarantine a high-risk ingest or to augment
    a flagged retrieval's provenance. This separation keeps
    security composable: a paranoid user mounts it AND a policy
    plugin that acts on the verdicts; a casual user mounts it for
    observability only.

    The PermissionGate is the exception: it IS enforced by default
    (default-deny) — but only callers who consult the gate's
    ``can_read`` / ``can_exec`` verdicts are affected. The plugin
    does not monkeypatch ``os`` or ``subprocess``. Plugins that
    want to execute code MUST call ``self.permission.can_exec(cmd)``
    first; if they ignore the verdict, the gate can't help them.
    That's by design: composition, not coercion.
    """

    name = "security"
    # We don't strictly require "memory" — security can run
    # standalone on raw text. But if "memory" is available, we
    # wire our retrieval check to use its embedder.
    inject: list[str] = []

    def __init__(self, mind_threshold: float = 0.85,
                 quarantine_high_risk: bool = True,
                 enable_permission_gate: bool = True) -> None:
        self.mind_threshold = mind_threshold
        self.quarantine_high_risk = quarantine_high_risk
        self.enable_permission_gate = enable_permission_gate
        self._embedder = None
        # The PermissionGate is constructed eagerly so users can
        # start granting immediately after mount, even before any
        # memory or audit_log service is wired.
        self.permission: PermissionGate | None = (
            PermissionGate() if enable_permission_gate else None)

    def apply(self, ctx) -> None:
        # Try to grab the memory service's embedder for MIND diversity
        # checks. If not mounted, MIND will use a fallback embedder.
        try:
            mem_service = ctx.inject("memory")
            mem = mem_service.get("memory") if isinstance(
                mem_service, dict) else mem_service
            self._embedder = getattr(mem, "palace", None) and \
                getattr(mem.palace, "embedder", None)
            # If memory has an audit_log, wire the permission gate to it.
            if self.permission is not None:
                audit = getattr(mem, "audit_log", None)
                if audit is not None:
                    self.permission.audit = audit
        except Exception:
            self._embedder = None

        ctx.service("security", self)
        # No reversible side effects — pure functions
        # ctx.effect(lambda: None) is not needed

    # ---------------------------- ingest scan -----------------------

    def scan_ingest(self, text: str) -> minja.InjectionVerdict:
        """Run MINJA pattern scan on raw text before extraction.

        Returns an InjectionVerdict with .risk in {none, medium, high}
        and .quarantined set if high-risk patterns matched AND
        ``self.quarantine_high_risk`` is True.

        The Memory.write path already runs this internally; this
        method is for callers that want to preview the verdict
        BEFORE deciding whether to call mem.add().
        """
        return minja.scan(text, quarantine_high=self.quarantine_high_risk)

    # ---------------------------- retrieval scan --------------------

    def scan_retrieval(self, facts: list, embedder=None) -> mind.MINDVerdict:
        """Run MIND diversity check on a retrieval result set.

        ``facts`` can be a list of Fact objects (cortexm.trace.fact.Fact)
        OR a list of dicts (mem.search output). We adapt internally.

        Returns a MINDVerdict with .diversity ∈ [0,1], .flagged=True
        if diversity > threshold.
        """
        if not facts:
            return mind.MINDVerdict(
                diversity=0.0, flagged=False,
                threshold=self.mind_threshold,
                n_facts=0,
                reason="no facts to check",
                fact_ids=[])

        # Convert dicts → Fact-like objects the MIND check can score
        fact_objs = []
        from cortexm.trace.fact import Fact
        for f in facts:
            if isinstance(f, Fact):
                fact_objs.append(f)
            elif isinstance(f, dict):
                # mem.search returns dicts with at least 'memory' field
                fact_objs.append(Fact(
                    id=f.get("id", ""),
                    user_id=f.get("user_id", ""),
                    subject=f.get("subject", "") or "",
                    relation=f.get("relation", "") or "memory",
                    value=f.get("memory", "") or f.get("value", ""),
                    valid_from=f.get("valid_from"),
                    valid_to=f.get("valid_to"),
                    confidence=f.get("confidence", 1.0),
                ))
            else:
                # Fact-like — pass through
                fact_objs.append(f)

        emb = embedder or self._embedder
        if emb is None:
            # No embedder available — can't run MIND, return unknown
            return mind.MINDVerdict(
                diversity=0.0, flagged=False,
                threshold=self.mind_threshold,
                n_facts=len(fact_objs),
                reason="no embedder available — MIND diversity check "
                        "skipped (mount the structured plugin or pass "
                        "embedder= to enable)",
                fact_ids=[getattr(f, "id", str(i)) for i, f in
                          enumerate(fact_objs)])

        return mind.mind_check(fact_objs, emb,
                               threshold=self.mind_threshold)

    # ---------------------------- combined verdict -----------------

    def scan(self, *, text: str | None = None,
             facts: list | None = None) -> SecurityVerdict:
        """Run both MINJA + MIND and return a combined verdict.

        Convenience method for the trajectory viewer / audit log
        which wants a single object summarizing the security state.
        """
        v = SecurityVerdict()
        if text is not None:
            iv = self.scan_ingest(text)
            v.ingest_risk = iv.risk
            v.ingest_quarantined = iv.quarantined
            v.ingest_rules = iv.rules
        if facts is not None:
            mv = self.scan_retrieval(facts)
            v.retrieval_diversity = mv.diversity
            v.retrieval_flagged = mv.flagged
            v.retrieval_reason = mv.reason
        return v


__all__ = ["SecurityPlugin", "SecurityVerdict"]
