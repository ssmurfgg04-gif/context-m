"""Zero-Knowledge Memory Proofs (ZK-lite).

Proves a retrieved fact satisfies a query WITHOUT revealing its content
to the LLM: a Merkle membership proof over the tamper-evident leaf set
(blake3 source hashes) plus an HMAC attestation binding
{statement, root, timestamp}. The LLM receives only
``[ZK-Proof: match on <relation> verified. Content redacted.]``.

Honest scope: this is a commit-and-prove membership attestation — full
ZK-SNARKs over the similarity predicate are on the roadmap (the binary
codec's Hamming-distance similarity is a natural circuit candidate;
HRR circular convolution is a group operation, the algebraic
requirement standard cosine similarity cannot satisfy).
"""

from __future__ import annotations

import secrets
import time

from cortexm.errors import VerificationError
from cortexm.security.hashes import (HashProvider, attest, merkle_proof,
                                       merkle_verify, verify_attest)


class ZKProver:
    def __init__(self, store, reader, provider: HashProvider | None = None) -> None:
        self.store = store
        self.reader = reader
        self.hasher = provider or store.hasher
        key = self.store.kv_get("ZK_KEY")
        if not key:
            key = secrets.token_hex(32)
            self.store.kv_set("ZK_KEY", key)
        self._key = bytes.fromhex(key)
        self._leaf_cache: tuple[str, list[str]] = ("", [])

    # ------------------------------------------------------------------
    def _leaves(self) -> list[str]:
        head = self.store.head() or ""
        if self._leaf_cache[0] != head or not self._leaf_cache[1]:
            hashes = self.store.active_fact_hashes()
            leaves = [f"{fid}:{h}" for fid, h in hashes]
            # leaf hash -> hex digest for merkle
            leaves = [self.hasher.hash_text(l) for l in leaves]
            self._leaf_cache = (head, leaves)
        return self._leaf_cache[1]

    # ------------------------------------------------------------------
    def prove(self, query: str, *, user_id: str = "default",
              threshold: float = 0.2) -> dict:
        """Retrieve top match and produce a content-free proof."""
        result = self.reader.search(query, user_id=user_id, k=1)
        if not result.facts:
            raise VerificationError("no matching fact to prove")
        f = result.facts[0]
        leaves = self._leaves()
        idx = None
        want = self.hasher.hash_text(f"{f.id}:{f.source_hash}")
        for i, leaf in enumerate(leaves):
            if leaf == want:
                idx = i
                break
        if idx is None:
            raise VerificationError("fact not in active leaf set")
        root, path = merkle_proof(self.hasher, leaves, idx)
        score = result.scores.get(f.id, 0.0)
        if score < threshold:
            raise VerificationError(
                f"similarity {score:.3f} below threshold {threshold}")
        statement = (f"EXISTS fact f in Trace: sim(query, f) >= {threshold} "
                     f"AND blake3(source(f)) = {f.source_hash[:16]}... "
                     f"(content redacted)")
        ts = time.time()
        tag = attest(self.hasher, self._key, f"{statement}|{root}|{ts}")
        return {
            "statement": statement,
            "fact_commitment": f.source_hash,       # content-free commitment
            "leaf_commitment": want,                # merkle leaf hash
            "relation": f.relation,                  # safe to disclose
            "sim_score": round(score, 4),
            "merkle_root": root,
            "merkle_path": path,
            "timestamp": ts,
            "attestation": tag,
            "llm_view": f"[ZK-Proof: high-confidence match on '{f.relation}' "
                        f"verified (score {score:.2f}). Content redacted.]",
        }

    # ------------------------------------------------------------------
    def verify(self, proof: dict) -> bool:
        """Verify membership + attestation. Returns True if sound."""
        if not verify_attest(self.hasher, self._key,
                             f"{proof['statement']}|{proof['merkle_root']}|"
                             f"{proof['timestamp']}",
                             proof["attestation"]):
            return False
        leaf = proof.get("leaf_commitment") or proof["fact_commitment"]
        return merkle_verify(self.hasher, leaf,
                             proof["merkle_path"], proof["merkle_root"])

    def verify_membership(self, leaf_hex: str, proof: dict) -> bool:
        """Full membership verification for a known leaf commitment."""
        return merkle_verify(self.hasher, leaf_hex, proof["merkle_path"],
                             proof["merkle_root"])
