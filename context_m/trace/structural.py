"""Deterministic multi-hop structural queries via symbolic Trace + VSA unbind.

Complementary to PPR (probabilistic graph diffusion):
  PPR answers: "what else might be relevant?"  — fuzzy, associative
  structural_query answers: "exactly follow this chain."  — deterministic

Inspired by HMS's multiHopQuery API:

    // "Who is John's grandfather?" (father → father)
    const grandpa = await hms.multiHopQuery('john', ['father', 'father']);

Algorithm:
  for each relation in the chain:
    1. Symbolic lookup: exact match in the Trace (subject=current, rel=r)
       — if a single fact matches, take its value as the next current
       — if multiple match (ambiguous), take the highest-confidence one
    2. VSA fallback: if no symbolic match, unbind the role hologram
       from the entity hologram in the palace, search the result for
       the nearest stored item vector (Hopfield cleanup). The nearest
       item becomes the next current.

Returns a `StructuralQueryResult` with the final value + the chain
of facts traversed. Ambiguous hops or fallback hops are flagged so
the caller knows the confidence of the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Hop:
    """One hop in a structural query chain."""
    relation: str
    subject: str
    value: str
    fact_id: str = ""
    confidence: float = 0.0
    via: str = ""           # "symbolic" | "vsa_fallback" | "abstain"
    ambiguous: bool = False  # multiple candidates matched
    alternatives: list[str] = field(default_factory=list)


@dataclass
class StructuralQueryResult:
    """Result of a multi-hop structural query."""
    start_entity: str
    relation_chain: list[str]
    hops: list[Hop] = field(default_factory=list)
    final_value: str = ""
    success: bool = False
    failure_reason: str = ""
    confidence: float = 0.0


def structural_query(store, palace, start_entity: str,
                      relation_chain: list[str],
                      user_id: str | None = None,
                      allow_hypotheses: bool = False,
                      vsa_fallback: bool = True) -> StructuralQueryResult:
    """Deterministic multi-hop via symbolic Trace + VSA unbinding.

    Args:
        store: TraceStore
        palace: MemoryPalace (for VSA fallback)
        start_entity: the entity to start from
        relation_chain: list of relations to follow in order
        user_id: restrict to a user (None = all users)
        allow_hypotheses: if True, also follow HYPOTHESIZED_BY edges
                          (derived facts from the cognition engine)
        vsa_fallback: if True, when no symbolic match, use VSA unbind
                       + Hopfield cleanup to propose a filler

    Returns:
        StructuralQueryResult with hops + final value + confidence
    """
    res = StructuralQueryResult(
        start_entity=start_entity, relation_chain=list(relation_chain))
    current = start_entity
    confidence_product = 1.0

    for rel in relation_chain:
        # 1. Symbolic lookup in the Trace
        where = ("subject=? AND relation=? AND is_active=1 "
                 "AND quarantined=0")
        args: list = [current, rel]
        if user_id is not None:
            where += " AND user_id=?"
            args.append(user_id)
        if not allow_hypotheses:
            where += " AND is_derived=0"
        rows = store.conn.execute(
            f"SELECT id, value, confidence FROM facts WHERE {where} "
            f"ORDER BY confidence DESC, valid_from DESC", args).fetchall()

        if rows:
            # single match — clean hop
            # SELECT id, value, confidence — columns 0, 1, 2
            row = rows[0]
            ambiguous = len(rows) > 1
            hop = Hop(
                relation=rel, subject=current, value=row[1],
                fact_id=row[0], confidence=row[2] if row[2] is not None else 0.5,
                via="symbolic", ambiguous=ambiguous,
                alternatives=[r[1] for r in rows[1:5]])
            res.hops.append(hop)
            confidence_product *= hop.confidence
            current = row[1]
            continue

        # 2. VSA fallback: unbind role from entity, search palace
        if vsa_fallback and palace is not None:
            try:
                # encode current entity into a hologram, then unbind
                # the role hologram to get the (noisy) filler hologram
                # and search the palace for the nearest stored item.
                entity_vec = palace._encode_entity(current)
                role_vec = palace.vsa.role_vector(rel)
                probe = palace.vsa.unbind(role_vec, entity_vec)
                # nearest neighbor in the palace
                ids, scores = palace.search(probe, k=3)
                if ids:
                    best_id = ids[0]
                    best_score = scores[0]
                    # fetch the value of the best_id fact
                    fact_row = store.conn.execute(
                        "SELECT value, confidence FROM facts WHERE id=?",
                        (best_id,)).fetchone()
                    if fact_row and best_score > 0.30:
                        hop = Hop(
                            relation=rel, subject=current,
                            value=fact_row[0], fact_id=best_id,
                            confidence=min(0.49, best_score * 0.5),
                            via="vsa_fallback",
                            alternatives=[
                                ids[i] for i in range(1, len(ids))])
                        res.hops.append(hop)
                        confidence_product *= hop.confidence
                        current = fact_row[0]
                        continue
            except Exception:
                pass  # palace might not have role_vec for `rel`, etc.

        # 3. Abstain — no match found, halt the chain
        res.hops.append(Hop(
            relation=rel, subject=current, value="",
            via="abstain"))
        res.success = False
        res.failure_reason = (
            f"no fact matching ({current!r}, {rel!r}) in the trace"
            + ("" if vsa_fallback else " (and VSA fallback disabled)"))
        res.confidence = confidence_product
        return res

    res.final_value = current
    res.success = True
    res.confidence = confidence_product
    return res


def multi_hop_chain(store, start_entity: str, relation: str,
                    depth: int = 2, **kwargs) -> StructuralQueryResult:
    """Convenience: walk the same relation `depth` times.

    E.g. multi_hop_chain(store, 'alice', 'father', depth=2) answers
    "who is Alice's father's father?" (i.e. grandfather).
    """
    return structural_query(
        store, kwargs.get("palace"),
        start_entity=start_entity,
        relation_chain=[relation] * depth,
        user_id=kwargs.get("user_id"),
        allow_hypotheses=kwargs.get("allow_hypotheses", False),
        vsa_fallback=kwargs.get("vsa_fallback", True),
    )


__all__ = [
    "Hop", "StructuralQueryResult",
    "structural_query", "multi_hop_chain",
]
