"""Vector Symbolic Architecture algebra.

Modes (config ``vsa_mode``):
  * ``perm`` — permutation binding (default). bind(role, filler) permutes
    the filler with a role-seeded permutation. Similarity-preserving,
    cheap (index shuffle), and directly portable to binary HDC
    hardware (XOR/permutation ops) per the plan's edge roadmap.
  * ``conv`` — Holographic Reduced Representations (Plate 1995):
    circular-convolution binding via FFT, involution-based unbinding.
  * ``bag`` — role-weighted superposition only (ablation baseline).

Every fact hologram = role-bound components + λ-weighted lexical
superposition. The λ term keeps free-text queries effective while the
bound terms carry structure for probe queries.
"""

from __future__ import annotations

import numpy as np

from context_m.util import h64 as _h64  # deterministic seeded hashing

ROLE_WEIGHTS = {"S": 1.0, "R": 1.0, "V": 1.0}


def _norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class VSA:
    def __init__(self, dims: int = 768, mode: str = "perm",
                 seed: int = 0x0C0FFEE, lexical_lambda: float = 0.6) -> None:
        self.dims = dims
        self.mode = mode
        self.seed = seed
        self.lam = lexical_lambda
        self._perms: dict[str, np.ndarray] = {}
        self._inperms: dict[str, np.ndarray] = {}
        self._roles: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------- roles
    def perm(self, role: str) -> np.ndarray:
        p = self._perms.get(role)
        if p is None:
            rng = np.random.default_rng(_h64(f"perm:{role}", self.seed) & 0xFFFFFFFF)
            p = rng.permutation(self.dims).astype(np.int32)
            self._perms[role] = p
            inv = np.empty_like(p)
            inv[p] = np.arange(self.dims, dtype=np.int32)
            self._inperms[role] = inv
        return p

    def inv_perm(self, role: str) -> np.ndarray:
        self.perm(role)
        return self._inperms[role]

    def role_vec(self, role: str) -> np.ndarray:
        v = self._roles.get(role)
        if v is None:
            rng = np.random.default_rng(_h64(f"role:{role}", self.seed) & 0xFFFFFFFF)
            v = rng.standard_normal(self.dims).astype(np.float32)
            v /= max(float(np.linalg.norm(v)), 1e-9)
            self._roles[role] = v
        return v

    # ------------------------------------------------------- binding ops
    def bind(self, role: str, filler: np.ndarray) -> np.ndarray:
        if self.mode == "perm":
            return filler[self.perm(role)]
        if self.mode == "conv":
            a = np.fft.rfft(filler)
            b = np.fft.rfft(self.role_vec(role))
            return np.fft.irfft(a * b, n=self.dims).astype(np.float32)
        return (ROLE_WEIGHTS.get(role, 1.0) * filler).astype(np.float32)

    def unbind(self, role: str, h: np.ndarray) -> np.ndarray:
        if self.mode == "perm":
            return h[self.inv_perm(role)]
        if self.mode == "conv":
            r = self.role_vec(role)
            inv = np.concatenate(([r[0]], r[1:][::-1]))  # involution
            a = np.fft.rfft(h)
            b = np.fft.rfft(inv)
            return np.fft.irfft(a * b, n=self.dims).astype(np.float32)
        return h / ROLE_WEIGHTS.get(role, 1.0)

    @staticmethod
    def bundle(vecs: list[np.ndarray]) -> np.ndarray:
        return _norm(np.sum(vecs, axis=0).astype(np.float32))

    # ---------------------------------------------------- fact encoding
    def encode_fact(self, s_vec: np.ndarray, r_vec: np.ndarray,
                    v_vec: np.ndarray) -> np.ndarray:
        lex = _norm(s_vec + r_vec + v_vec)
        if self.mode == "bag":
            return _norm(ROLE_WEIGHTS["S"] * s_vec + ROLE_WEIGHTS["R"] * r_vec
                         + ROLE_WEIGHTS["V"] * v_vec)
        bound = self.bundle([
            self.bind("S", s_vec), self.bind("R", r_vec), self.bind("V", v_vec)])
        return _norm(bound + self.lam * lex)

    def probe(self, s_vec: np.ndarray | None = None,
              r_vec: np.ndarray | None = None,
              v_vec: np.ndarray | None = None,
              lexical: np.ndarray | None = None) -> np.ndarray:
        """Structured probe: approximate fact hologram from known roles."""
        parts: list[np.ndarray] = []
        if s_vec is not None:
            parts.append(self.bind("S", s_vec))
        if r_vec is not None:
            parts.append(self.bind("R", r_vec))
        if v_vec is not None:
            parts.append(self.bind("V", v_vec))
        if not parts:
            return _norm(lexical if lexical is not None else np.zeros(self.dims, np.float32))
        probe = self.bundle(parts)
        if lexical is not None and self.mode != "bag":
            probe = _norm(probe + self.lam * _norm(lexical))
        return probe

    def unbind_role(self, h: np.ndarray, role: str) -> np.ndarray:
        """Extract the approximate filler bound to ``role`` (audit path)."""
        return _norm(self.unbind(role, h))
