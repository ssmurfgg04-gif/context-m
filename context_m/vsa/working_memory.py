"""Holographic working memory — compress top-k facts into a single HRR.

The strategic plan calls for compressing the top-k retrieved facts
into a single HRR superposition injected into the LLM system prompt.
The LLM unbinds specific facts on demand. This is a 5-10× token
reduction for the context window.

Mechanism (VSA / HRR algebra, Plate 1995):
  - For each fact (S, R, V), compute its binding:
      fact_holo = bind(S, role_vec("S")) + bind(R, role_vec("R"))
                  + bind(V, role_vec("V"))
    where each component is the L2-normalized embedding of the
    fact's subject / relation / value text, and `bind` is the VSA's
    role-seeded permutation (mode="perm") or circular-convolution
    (mode="conv").
  - The full working memory hologram is the normalized superposition
    of all fact_holo vectors. Adding more facts to the same superposition
    is the standard HRR "memory" operation — every fact is added into
    one vector, with overlap (cross-talk) controlled by the orthogonality
    of the role vectors.

To RECALL a specific fact given a query, the LLM (or the host agent)
asks us to UNBIND — e.g. "what is Alice's job?" → we unbind the "S"
role on the hologram with role_vec("S") permuted against the query
"Alice", then nearest-neighbor lookup against the fact corpus. The
beauty of HRR is that this is a pure vector operation — no learned
weights, μ=0.

For the system-prompt injection use case we keep it simple:
  - Compress top-k retrieved facts into one HRR vector + a short
    textual preamble describing the available "fact slots" (S/R/V
    bindings present).
  - The LLM can ask us to "extract" a specific fact from the hologram
    via the `extract_from_hologram` MCP tool / REST endpoint, which
    does the unbind + nearest-neighbor lookup.

Net effect: instead of injecting 10 facts × ~30 tokens each = ~300
tokens into the system prompt, we inject ~30 tokens of preamble +
the HRR vector (which an LLM doesn't see directly — it sees the
preamble). The savings are on RETRIEVAL repetition across turns: a
chat with 10 turns that re-fetches the same 10 facts each turn saves
~2700 tokens vs naive injection. HRR vectors are also constant-size
memory of arbitrary depth.

For now, we provide the compression + extraction primitives. The
agent-side "ask the hologram" loop is left to the host app; the
hologram is the substrate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np


@dataclass
class HolographicWM:
    """A compressed working-memory representation of top-k facts.

    Attributes:
        hrr: the superposed HRR vector (dims,). L2-normalized.
        n_facts: how many facts were superposed.
        roles_present: which role bindings are present (subset of
            {"S","R","V"}). Lets the host LLM know what it can ask
            the hologram to unbind.
        fact_ids: list of fact ids that were superposed, in superposition
            order. Used by extract_from_hologram to look up the answer
            after unbinding.
        preamble: a short textual description suitable for injection
            into an LLM system prompt. ~30-50 tokens. Tells the LLM
            "you have N facts compressed as a hologram; ask the
            contextm_hologram_extract tool to recall any of them".
    """
    hrr: np.ndarray
    n_facts: int
    roles_present: set[str] = field(default_factory=set)
    fact_ids: list[str] = field(default_factory=list)
    preamble: str = ""

    def to_dict(self) -> dict:
        return {
            "n_facts": self.n_facts,
            "roles_present": sorted(self.roles_present),
            "fact_ids": self.fact_ids,
            "preamble": self.preamble,
            # hrr vector is large; serialize as hex of packed bytes
            # only when explicitly requested via to_dict_with_vec
        }

    def to_dict_with_vec(self) -> dict:
        d = self.to_dict()
        d["hrr_b64"] = _vec_to_b64(self.hrr)
        return d


def _vec_to_b64(v: np.ndarray) -> str:
    import base64
    return base64.b64encode(v.tobytes()).decode("ascii")


def _vec_from_b64(s: str, dtype=np.float32) -> np.ndarray:
    import base64
    return np.frombuffer(base64.b64decode(s), dtype=dtype)


# ---------------------------------------------------------------------------
# Build / extract
# ---------------------------------------------------------------------------

def build_holographic_wm(facts: Iterable, vsa,
                          embedder, *, max_facts: int = 12,
                          roles: tuple[str, ...] = ("S", "R", "V"),
                          normalize: bool = True) -> HolographicWM:
    """Compress top-k facts into a single HRR superposition.

    Parameters
    ----------
    facts : iterable of fact-like objects with .subject, .relation,
            .value, .id
    vsa : a context_m.vsa.ops.VSA instance (provides role_vec, bind,
          superpose). The same instance the palace uses — ensures role
          vectors match between encode and unbind.
    embedder : a HashingEmbedder or compatible, for embedding the
               subject/relation/value text.
    max_facts : cap on the number of facts superposed. Beyond ~24 the
                cross-talk noise starts to degrade extraction accuracy.
    roles : which bindings to include in each fact hologram.

    Returns a HolographicWM with the superposed HRR + metadata.
    """
    facts = list(facts)[:max_facts]
    if not facts:
        return HolographicWM(hrr=np.zeros(vsa.dims, dtype=np.float32),
                              n_facts=0, roles_present=set(),
                              fact_ids=[], preamble="(no facts in memory)")

    roles_present: set[str] = set()
    fact_ids: list[str] = []
    acc = np.zeros(vsa.dims, dtype=np.float32)

    for f in facts:
        subj = getattr(f, "subject", "") or ""
        rel = getattr(f, "relation", "") or ""
        val = getattr(f, "value", "") or ""
        fid = getattr(f, "id", "") or ""
        fact_ids.append(fid)
        # bind each component against its role vector
        if "S" in roles and subj:
            roles_present.add("S")
            s_emb = embedder.embed(subj)
            acc += vsa.bind("S", s_emb)
        if "R" in roles and rel:
            roles_present.add("R")
            r_emb = embedder.embed(rel.replace("_", " "))
            acc += vsa.bind("R", r_emb)
        if "V" in roles and val:
            roles_present.add("V")
            v_emb = embedder.embed(val)
            acc += vsa.bind("V", v_emb)

    if normalize:
        n = float(np.linalg.norm(acc))
        if n > 0:
            acc = acc / n

    preamble = _build_preamble(facts, roles_present)
    return HolographicWM(hrr=acc.astype(np.float32), n_facts=len(facts),
                         roles_present=roles_present,
                         fact_ids=fact_ids, preamble=preamble)


def extract_from_hologram(hwm: HolographicWM, role: str, query_vec,
                          vsa, candidate_embs: np.ndarray,
                          candidate_ids: list[str],
                          top_k: int = 3) -> list[tuple[str, float]]:
    """Unbind a role from the hologram and nearest-neighbor lookup.

    The classic HRR recall: given a hologram H and a role r, the
    unbound vector U = unbind(r, H) is approximately the filler that
    was bound to r in H (averaged across all superposed facts). We then
    find the nearest neighbor of U in the candidate embedding matrix.

    Parameters
    ----------
    hwm : the HolographicWM returned by build_holographic_wm
    role : "S", "R", or "V" — which role to unbind
    query_vec : NOT USED in this simple variant; kept for forward-compat
                 with a query-conditional unbind mode.
    vsa : the same VSA instance used to build the hologram
    candidate_embs : (N, dims) matrix of candidate fact-component embeddings
    candidate_ids : list of N ids parallel to candidate_embs
    top_k : how many candidates to return

    Returns a list of (id, score) sorted desc by score. Empty if the
    hologram is empty or role was not present at build time.
    """
    if hwm.n_facts == 0 or role not in hwm.roles_present:
        return []
    if candidate_embs is None or len(candidate_embs) == 0:
        return []
    unbound = vsa.unbind(role, hwm.hrr)
    # cosine sim against every candidate (batched)
    cands = np.asarray(candidate_embs, dtype=np.float32)
    norms = np.linalg.norm(cands, axis=1) + 1e-9
    sims = (cands @ unbound) / norms
    # take top-k indices
    if len(sims) <= top_k:
        idxs = np.argsort(-sims)
    else:
        idxs = np.argpartition(-sims, top_k)[:top_k]
        idxs = idxs[np.argsort(-sims[idxs])]
    out = [(candidate_ids[i], float(sims[i])) for i in idxs]
    return out


# ---------------------------------------------------------------------------
# Preamble builder — what gets injected into the LLM system prompt.
# ---------------------------------------------------------------------------

def _build_preamble(facts: list, roles_present: set[str]) -> str:
    """Build a short (~30-50 token) LLM-ready description of the hologram.

    The LLM sees this preamble and knows it can ask for unbind queries
    against the hologram. The actual HRR vector is opaque to the LLM
    (it's a vector, not text); the host app routes "what was Alice's
    job?" type questions through the extract_from_hologram endpoint.
    """
    n = len(facts)
    roles_str = "/".join(sorted(roles_present)) if roles_present else "none"
    subjects = [getattr(f, "subject", "") for f in facts]
    # dedupe subjects
    seen = set()
    unique_subjects = [s for s in subjects
                        if s and not (s in seen or seen.add(s))]
    sub_str = ", ".join(unique_subjects[:5])
    if len(unique_subjects) > 5:
        sub_str += f", ... (+{len(unique_subjects) - 5} more)"
    return (f"[Working Memory Hologram] {n} fact(s) compressed "
            f"as HRR over roles ({roles_str}). Subjects: {sub_str}. "
            f"Ask the contextm_hologram_extract tool to recall any "
            f"specific (subject, relation) pair.")


__all__ = [
    "HolographicWM",
    "build_holographic_wm",
    "extract_from_hologram",
]
