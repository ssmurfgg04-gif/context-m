"""Typed edge vocabulary for the Trace.

arXiv:2601.15311 (Aeon) formalizes an episodic Trace with typed edges:
CAUSAL, NEXT, REFERS_TO. Context-M already had EXTRACTED_FROM /
CONTRADICTS / TEMPORALLY_PRECEDED_BY — this module adds the missing
types and centralizes the vocabulary so:

- write-side code (writer.py / query_extract.py / consolidate.py) has
  one place to discover the canonical edge kinds
- read-side code (reader.py / ppr.py) can enumerate them by purpose
  ("causal", "temporal", "provenance", "semantic_ref") without hard-
  coding strings scattered across the codebase
- the dreaming / consolidation pass can walk the typed graph
  intentionally (e.g. merge facts joined by NEXT, summarize chains
  linked by CAUSAL)

Edge kind -> direction convention:
    src -> dst means "src is the cause / antecedent / referring node"
    e.g. CAUSAL(fact_A, fact_B) reads "fact_A caused fact_B" (A is
    the cause, B is the effect). This matches Aeon's convention.

The kind column on `edges` is TEXT — adding new kinds requires no
schema migration, only a helper here that knows how to wire them.
"""
from __future__ import annotations

from typing import Final


# ---------- Canonical edge vocabulary (centralized) --------------------

# provenance: where a fact came from
EXTRACTED_FROM: Final[str] = "EXTRACTED_FROM"   # fact -> chunk
MENTIONS: Final[str] = "MENTIONS"                 # fact -> entity mention

# temporal / causal ordering
TEMPORALLY_PRECEDED_BY: Final[str] = "TEMPORALLY_PRECEDED_BY"  # fact -> fact (next is newer)
CAUSAL: Final[str] = "CAUSAL"                      # fact -> fact (cause -> effect)
NEXT: Final[str] = "NEXT"                          # fact -> fact (narrative next, no causal claim)

# truth maintenance
CONTRADICTS: Final[str] = "CONTRADICTS"            # fact -> fact (new contradicts old)
SUPERSEDES: Final[str] = "SUPERSEDES"              # fact -> fact (new replaces old)
RETRACTED_BY: Final[str] = "RETRACTED_BY"          # fact -> fact (retired by retraction)
MERGED_WITH: Final[str] = "MERGED_WITH"            # fact -> fact (merged into target)

# semantic cross-references (Aeon's REFERS_TO)
REFERS_TO: Final[str] = "REFERS_TO"                # fact -> fact/concept (episodic → atlas)
SAME_PERSON: Final[str] = "SAME_PERSON"            # alias fact -> canonical fact (e.g. "Priya" → "Priya Johnson")

# HMS cognition engine edges — produced by cortexm.cognition.*.
# HYPOTHESIZED_BY: hypothesis fact -> supporting fact(s) (reasoning chain)
# PROMOTED_FROM:   user-confirmed fact -> hypothesis origin (truth
#                   maintenance — when a hypothesis is verified by
#                   user input, the new fact links back to its origin)
# ABSTRACTS:       abstraction prototype -> member (categorization)
# INSTANTIATES:    member -> abstraction prototype (inverse)
# ANALOGOUS_TO:    relation_a -> relation_b (structural isomorphism)
HYPOTHESIZED_BY: Final[str] = "HYPOTHESIZED_BY"
PROMOTED_FROM: Final[str] = "PROMOTED_FROM"
ABSTRACTS: Final[str] = "ABSTRACTS"
INSTANTIATES: Final[str] = "INSTANTIATES"
ANALOGOUS_TO: Final[str] = "ANALOGOUS_TO"

# All kinds in one place for enumeration / display
ALL_KINDS: Final[tuple[str, ...]] = (
    EXTRACTED_FROM, MENTIONS,
    TEMPORALLY_PRECEDED_BY, CAUSAL, NEXT,
    CONTRADICTS, SUPERSEDES, RETRACTED_BY, MERGED_WITH,
    REFERS_TO, SAME_PERSON,
    HYPOTHESIZED_BY, PROMOTED_FROM,
    ABSTRACTS, INSTANTIATES, ANALOGOUS_TO,
)

# Group by purpose — used by the reader / PPR / consolidator
PROVENANCE_EDGES: Final[tuple[str, ...]] = (EXTRACTED_FROM, MENTIONS)
TEMPORAL_EDGES: Final[tuple[str, ...]] = (TEMPORALLY_PRECEDED_BY, NEXT)
CAUSAL_EDGES: Final[tuple[str, ...]] = (CAUSAL,)
TRUTH_MAINTENANCE_EDGES: Final[tuple[str, ...]] = (
    CONTRADICTS, SUPERSEDES, RETRACTED_BY, MERGED_WITH,
    PROMOTED_FROM)
SEMANTIC_REF_EDGES: Final[tuple[str, ...]] = (
    REFERS_TO, SAME_PERSON,
    ABSTRACTS, INSTANTIATES, ANALOGOUS_TO)
COGNITION_EDGES: Final[tuple[str, ...]] = (
    HYPOTHESIZED_BY, PROMOTED_FROM,
    ABSTRACTS, INSTANTIATES, ANALOGOUS_TO)


# ---------- Helpers -----------------------------------------------------

def is_causal(kind: str) -> bool:
    """Does this edge type assert a causal / temporal antecedent?"""
    return kind in (CAUSAL, TEMPORALLY_PRECEDED_BY, NEXT)


def is_truth_maintenance(kind: str) -> bool:
    """Does this edge type mark a fact as no-longer-current?"""
    return kind in (CONTRADICTS, SUPERSEDES, RETRACTED_BY, MERGED_WITH)


def is_semantic_ref(kind: str) -> bool:
    """Does this edge type link two facts semantically (vs. structurally)?"""
    return kind in (REFERS_TO, SAME_PERSON)


def direction_convention(kind: str) -> str:
    """Return 'cause_to_effect' or 'new_to_old' or 'ref_to_target'.

    Used by reader/PPR to know which way to walk the edge for a given
    query intent (e.g. "why" queries walk CAUSAL src->dst; "what
    replaced this" queries walk SUPERSEDES dst->src).
    """
    if kind in (CAUSAL, TEMPORALLY_PRECEDED_BY, NEXT):
        return "cause_to_effect"  # src causes/precedes dst
    if kind in (CONTRADICTS, SUPERSEDES, RETRACTED_BY, MERGED_WITH):
        return "new_to_old"  # src is the newer fact, dst is the older
    if kind in (REFERS_TO, SAME_PERSON, MENTIONS):
        return "ref_to_target"
    return "out"


# ---------- CAUSAL / REFERS_TO writers --------------------------------

def wire_causal_edge(store, cause_fact_id: str, effect_fact_id: str,
                    reason: str = "") -> None:
    """Wire a CAUSAL edge from cause to effect.

    Used by the writer when a SUPERSEDE / retraction pattern fires —
    e.g. "I left Google in March" (cause) caused the older works_at
    fact to be retired (effect). Both edges are kept:
    SUPERSEDES marks the truth-maintenance side, CAUSAL marks the
    narrative cause. Reader can answer "why did X happen?" via CAUSAL
    traversal; it can answer "what's the current truth about X?" via
    SUPERSEDES traversal.
    """
    if cause_fact_id == effect_fact_id:
        return
    store.add_edge(cause_fact_id, effect_fact_id, CAUSAL,
                   {"reason": reason} if reason else None)


def wire_refers_to(store, ref_fact_id: str, target_id: str,
                   target_kind: str = "fact") -> None:
    """Wire a REFERS_TO edge from a referring fact to its target.

    Used by the writer / consolidator to link an episodic fact back to
    the atlas concept it refers to. For Context-M the "atlas" is the
    palace (the VSA vector space) — so REFERS_TO currently points at
    other facts (semantic cross-references) and may also point at
    chunk_ids (episodic → raw source) when called from the
    consolidator's branch-compression pass.
    """
    if ref_fact_id == target_id:
        return
    store.add_edge(ref_fact_id, target_id, REFERS_TO,
                   {"target_kind": target_kind})


def find_causal_chain(store, fact_id: str,
                      direction: str = "ancestors",
                      max_depth: int = 8) -> list[str]:
    """Walk CAUSAL edges from fact_id.

    direction='ancestors': walk src->dst backwards — return the chain
        of causes that led to this fact (effect).
    direction='descendants': walk src->dst forwards — return the chain
        of effects this fact caused.
    """
    seen: set[str] = {fact_id}
    out: list[str] = []
    frontier = [fact_id]
    for _ in range(max_depth):
        next_frontier: list[str] = []
        for fid in frontier:
            if direction == "ancestors":
                # find edges where dst=fid (this fact is the effect)
                rows = store.conn.execute(
                    "SELECT src FROM edges WHERE dst=? AND kind=?",
                    (fid, CAUSAL)).fetchall()
            else:
                # find edges where src=fid (this fact is the cause)
                rows = store.conn.execute(
                    "SELECT dst FROM edges WHERE src=? AND kind=?",
                    (fid, CAUSAL)).fetchall()
            for r in rows:
                other = r[0]
                if other in seen:
                    continue
                seen.add(other)
                out.append(other)
                next_frontier.append(other)
        if not next_frontier:
            break
        frontier = next_frontier
    return out


__all__ = [
    # kinds
    "EXTRACTED_FROM", "MENTIONS", "TEMPORALLY_PRECEDED_BY",
    "CAUSAL", "NEXT", "CONTRADICTS", "SUPERSEDES",
    "RETRACTED_BY", "MERGED_WITH", "REFERS_TO", "SAME_PERSON",
    "HYPOTHESIZED_BY", "PROMOTED_FROM",
    "ABSTRACTS", "INSTANTIATES", "ANALOGOUS_TO",
    "ALL_KINDS", "PROVENANCE_EDGES", "TEMPORAL_EDGES",
    "CAUSAL_EDGES", "TRUTH_MAINTENANCE_EDGES", "SEMANTIC_REF_EDGES",
    "COGNITION_EDGES",
    # helpers
    "is_causal", "is_truth_maintenance", "is_semantic_ref",
    "direction_convention",
    # writers
    "wire_causal_edge", "wire_refers_to", "find_causal_chain",
]
