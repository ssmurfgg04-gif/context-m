"""Vector codecs — the cortexm-compress tier stack.

Per-vector storage at 768 dims (plan appendix "From 768 MB down to 8–96 MB"):

  codec    bytes  tier        binding/similarity
  -------  -----  ----------  ----------------------------------------
  int8      768   baseline    scalar symmetric quantization (Aeon-style)
  binary     96   edge        bipolar ±1, packed bits; XOR/permutation
                             ops map 1:1 to HDC hardware
  rabitq     96   ultra-edge  JL-rotation + binarization (RaBitQ-style),
                             provable angle preservation
  pq          8   cloud       product quantization M=8 x 8-bit codes,
                             ADC scoring via L1-resident lookup tables

All codecs expose: encode_packed / decoded / query_vec / scores /
to_bytes / from_bytes / corrupt, so the Palace and the tree index are
codec-agnostic. INT8 additionally stores a per-vector float16 scale
(aux). Binary supports triple-modular redundancy (TMR) for the
self-healing memory feature.
"""

from __future__ import annotations

import numpy as np

from context_m.errors import CodecError
from context_m.util import h64 as _h64


def _norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)


class BaseCodec:
    name = "base"

    @property
    def bytes_per_vector(self) -> int:
        raise NotImplementedError

    def encode_packed(self, vec: np.ndarray) -> np.ndarray: ...
    def decoded(self, packed: np.ndarray) -> np.ndarray: ...
    def query_vec(self, q: np.ndarray) -> np.ndarray: ...
    def scores(self, packed: np.ndarray, q: np.ndarray) -> np.ndarray: ...
    def to_bytes(self, packed_row: np.ndarray) -> bytes: ...
    def from_bytes(self, b: bytes) -> np.ndarray: ...
    def corrupt(self, packed_row: np.ndarray, rate: float,
                rng: np.random.Generator) -> np.ndarray: ...


# ---------------------------------------------------------------------------
class Int8Codec(BaseCodec):
    """Symmetric scalar quantization: q = round(127 * v / max|v|)."""

    name = "int8"
    uses_aux = True

    def __init__(self, dims: int) -> None:
        self.dims = dims

    @property
    def bytes_per_vector(self) -> int:
        return self.dims + 2  # + float16 scale

    def encode_packed(self, vec: np.ndarray) -> np.ndarray:
        v = np.asarray(vec, dtype=np.float32)
        m = float(np.max(np.abs(v))) or 1.0
        scale = 127.0 / m
        q = np.clip(np.rint(v * scale), -127, 127).astype(np.int8)
        return q

    @staticmethod
    def encode_scale(vec: np.ndarray) -> np.float16:
        v = np.asarray(vec, dtype=np.float32)
        m = float(np.max(np.abs(v))) or 1.0
        return np.float16(m / 127.0)

    def decoded(self, packed: np.ndarray, aux: np.ndarray | None = None) -> np.ndarray:
        arr = np.atleast_2d(packed).astype(np.float32)
        if aux is not None:
            arr = arr * np.atleast_1d(aux).astype(np.float32)[:, None]
        return arr

    def query_vec(self, q: np.ndarray) -> np.ndarray:
        return np.asarray(q, dtype=np.float32)

    def scores(self, packed: np.ndarray, q: np.ndarray,
               aux: np.ndarray | None = None) -> np.ndarray:
        if aux is None:
            raise CodecError("int8 scoring requires per-vector aux scales")
        arr = packed.astype(np.float32)
        if len(aux):
            arr = arr * np.atleast_1d(aux).astype(np.float32)[:, None]
        return arr @ np.asarray(q, dtype=np.float32)

    def to_bytes(self, packed_row: np.ndarray, scale: np.float16) -> bytes:
        return packed_row.tobytes() + np.float16(scale).tobytes()

    def from_bytes(self, b: bytes) -> tuple[np.ndarray, np.float16]:
        q = np.frombuffer(b[: self.dims], dtype=np.int8).copy()
        scale = np.frombuffer(b[self.dims:], dtype=np.float16)[0]
        return q, scale

    def corrupt(self, packed_row: np.ndarray, rate: float,
                rng: np.random.Generator) -> np.ndarray:
        n = len(packed_row)
        idx = rng.integers(0, n, size=max(1, int(n * rate)))
        out = packed_row.copy()
        noise = rng.integers(-30, 31, size=len(idx)).astype(np.int8)
        out[idx] = np.clip(out[idx].astype(np.int16) + noise, -127, 127).astype(np.int8)
        return out


# ---------------------------------------------------------------------------
class BinaryCodec(BaseCodec):
    """Bipolar {±1} hypervectors, 1 bit/dim — the HDC hardware target.

    Sparse embeddings binarize poorly (near-zero components produce
    correlated sign noise), so a fixed JL rotation densifies energy
    before binarization — the RaBitQ insight applied to the MAP model.
    Set ``rotate=False`` for raw bipolar-MAP semantics.
    """

    name = "binary"
    uses_aux = False

    def __init__(self, dims: int, tmr: bool = False, rotate: bool = True,
                 seed: int = 0x0C0FFEE) -> None:
        self.dims = dims
        self.words = dims // 8
        self.tmr = tmr
        self.rotate = rotate
        self.R = None
        if rotate:
            rng = np.random.default_rng(_h64("binary-rot", seed) & 0xFFFFFFFF)
            g = rng.standard_normal((dims, dims)).astype(np.float32)
            q, _ = np.linalg.qr(g)
            self.R = q.astype(np.float32)

    @property
    def bytes_per_vector(self) -> int:
        return self.words * (3 if self.tmr else 1)

    def encode_packed(self, vec: np.ndarray) -> np.ndarray:
        v = np.asarray(vec, dtype=np.float32)
        if self.R is not None:
            v = self.R @ v
        bits = (v > 0).astype(np.uint8)
        packed = np.packbits(bits)
        if not self.tmr:
            return packed
        return np.concatenate([packed, packed, packed])

    def decoded(self, packed: np.ndarray) -> np.ndarray:
        arr = np.atleast_2d(packed)
        if self.tmr:
            k = arr.shape[1] // 3
            arr = _tmr_majority(arr[:, :k], arr[:, k:2 * k], arr[:, 2 * k:])
        signs = (np.unpackbits(arr, axis=1, count=self.dims).astype(np.float32) * 2 - 1)
        return signs / np.sqrt(self.dims)

    def query_vec(self, q: np.ndarray) -> np.ndarray:
        v = np.asarray(q, dtype=np.float32)
        if self.R is not None:
            v = self.R @ v
        s = ((v > 0).astype(np.float32) * 2 - 1) / np.sqrt(self.dims)
        return s

    def scores(self, packed: np.ndarray, q: np.ndarray) -> np.ndarray:
        v = np.asarray(q, dtype=np.float32)
        if self.R is not None:
            v = self.R @ v
        qbits = np.packbits((v > 0).astype(np.uint8))
        arr = np.atleast_2d(packed)
        if self.tmr:
            k = arr.shape[1] // 3
            arr = _tmr_majority(arr[:, :k], arr[:, k:2 * k], arr[:, 2 * k:])
        x = np.bitwise_xor(arr, qbits[None, :])
        ham = np.bitwise_count(x).sum(axis=1).astype(np.float32)
        return 1.0 - 2.0 * ham / self.dims

    def to_bytes(self, packed_row: np.ndarray) -> bytes:
        return packed_row.tobytes()

    def from_bytes(self, b: bytes) -> np.ndarray:
        return np.frombuffer(b, dtype=np.uint8).copy()

    def corrupt(self, packed_row: np.ndarray, rate: float,
                rng: np.random.Generator) -> np.ndarray:
        out = packed_row.copy()
        n = len(out)
        idx = rng.integers(0, n, size=max(1, int(n * rate)))
        out[idx] ^= rng.integers(1, 256, size=len(idx)).astype(np.uint8)
        return out

    # TMR helpers -------------------------------------------------------
    def tmr_health(self, packed_row: np.ndarray) -> int:
        """Number of bit positions where the 3 copies disagree (corruption)."""
        k = len(packed_row) // 3
        a, b, c = packed_row[:k], packed_row[k:2 * k], packed_row[2 * k:]
        ab = np.bitwise_count(np.bitwise_xor(a, b)).sum()
        ac = np.bitwise_count(np.bitwise_xor(a, c)).sum()
        bc = np.bitwise_count(np.bitwise_xor(b, c)).sum()
        return int((ab + ac + bc) // 6)


def _tmr_majority(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Bit-level majority vote across 3 copies (self-healing memory)."""
    ab = a == b
    bc = b == c
    ac = a == c
    maj = np.where(ab, a, np.where(ac, a, c))
    maj = np.where(ab | bc | ac, maj, a)
    return maj.astype(np.uint8)


# ---------------------------------------------------------------------------
class RaBitQCodec(BaseCodec):
    """RaBitQ-style: fixed JL rotation, then binarization.

    The random orthogonal rotation concentrates energy so that sign
    binarization preserves angles far better than raw binarization
    (Wang et al., SIGMOD 2024/2025). Simplified here: unit-norm vectors,
    single global rotation, Hamming-based cosine estimate.
    """

    name = "rabitq"
    uses_aux = False

    def __init__(self, dims: int, seed: int = 0x0C0FFEE) -> None:
        self.dims = dims
        self.words = dims // 8
        rng = np.random.default_rng(_h64("rabitq-rot", seed) & 0xFFFFFFFF)
        g = rng.standard_normal((dims, dims)).astype(np.float32)
        q, _ = np.linalg.qr(g)
        self.R = q.astype(np.float32)

    @property
    def bytes_per_vector(self) -> int:
        return self.words

    def encode_packed(self, vec: np.ndarray) -> np.ndarray:
        rot = self.R @ np.asarray(vec, dtype=np.float32)
        return np.packbits((rot > 0).astype(np.uint8))

    def decoded(self, packed: np.ndarray) -> np.ndarray:
        signs = np.unpackbits(np.atleast_2d(packed), axis=1,
                              count=self.dims).astype(np.float32) * 2 - 1
        return signs / np.sqrt(self.dims)

    def query_vec(self, q: np.ndarray) -> np.ndarray:
        rot = self.R @ np.asarray(q, dtype=np.float32)
        s = ((rot > 0).astype(np.float32) * 2 - 1) / np.sqrt(self.dims)
        return s

    def scores(self, packed: np.ndarray, q: np.ndarray) -> np.ndarray:
        return self.scores_packed(packed, self.query_packed(q))

    def query_packed(self, q: np.ndarray) -> np.ndarray:
        rot = self.R @ np.asarray(q, dtype=np.float32)
        return np.packbits((rot > 0).astype(np.uint8))

    def scores_packed(self, packed: np.ndarray, qbits: np.ndarray) -> np.ndarray:
        x = np.bitwise_xor(np.atleast_2d(packed), qbits[None, :])
        ham = np.bitwise_count(x).sum(axis=1).astype(np.float32)
        return 1.0 - 2.0 * ham / self.dims

    def to_bytes(self, packed_row: np.ndarray) -> bytes:
        return packed_row.tobytes()

    def from_bytes(self, b: bytes) -> np.ndarray:
        return np.frombuffer(b, dtype=np.uint8).copy()

    def corrupt(self, packed_row: np.ndarray, rate: float,
                rng: np.random.Generator) -> np.ndarray:
        out = packed_row.copy()
        idx = rng.integers(0, len(out), size=max(1, int(len(out) * rate)))
        out[idx] ^= rng.integers(1, 256, size=len(idx)).astype(np.uint8)
        return out

    def serialize_rotation(self) -> bytes:
        return self.R.tobytes()


# ---------------------------------------------------------------------------
class PQCodec(BaseCodec):
    """Product quantization: M subspaces x 8-bit codes (8 bytes/vector).

    Cloud tier. Codebooks trained on the first N vectors (k-means per
    subspace, frozen afterwards); asymmetric distance computation via
    per-query lookup tables that stay L1-resident.
    """

    name = "pq"
    uses_aux = False

    def __init__(self, dims: int, m: int = 8, ks: int = 256,
                 train_threshold: int = 4096, seed: int = 0x0C0FFEE) -> None:
        if dims % m:
            raise CodecError("dims must be divisible by M")
        self.dims = dims
        self.m = m
        self.ks = ks
        self.sub = dims // m
        self.seed = seed
        self.train_threshold = train_threshold
        self.codebooks: np.ndarray | None = None   # (m, ks, sub)

    @property
    def trained(self) -> bool:
        return self.codebooks is not None

    @property
    def bytes_per_vector(self) -> int:
        return self.m

    def train(self, vecs: np.ndarray) -> None:
        from scipy.cluster.vq import kmeans2
        rng = np.random.default_rng(self.seed)
        data = np.asarray(vecs, dtype=np.float32)
        n = min(self.ks * 4, len(data))
        sample = data[rng.choice(len(data), size=n, replace=False)] if len(data) > n else data
        books = np.zeros((self.m, self.ks, self.sub), dtype=np.float32)
        for mi in range(self.m):
            chunk = sample[:, mi * self.sub:(mi + 1) * self.sub]
            k = min(self.ks, max(2, len(np.unique(chunk, axis=0))))
            try:
                cent, _ = kmeans2(chunk, k, iter=12, minit="++", seed=self.seed + mi)
            except Exception:
                cent = chunk[:k]
            books[mi, : len(cent)] = cent
        self.codebooks = books

    def set_codebooks(self, books: np.ndarray) -> None:
        self.codebooks = np.asarray(books, dtype=np.float32)

    def encode_packed(self, vec: np.ndarray) -> np.ndarray:
        if self.codebooks is None:
            raise CodecError("PQ codec not trained")
        v = np.asarray(vec, dtype=np.float32)
        codes = np.zeros(self.m, dtype=np.uint8)
        for mi in range(self.m):
            chunk = v[mi * self.sub:(mi + 1) * self.sub]
            sims = self.codebooks[mi] @ chunk
            codes[mi] = int(np.argmax(sims))
        return codes

    def decoded(self, packed: np.ndarray) -> np.ndarray:
        if self.codebooks is None:
            raise CodecError("PQ codec not trained")
        arr = np.atleast_2d(packed)
        out = np.zeros((arr.shape[0], self.dims), dtype=np.float32)
        for mi in range(self.m):
            out[:, mi * self.sub:(mi + 1) * self.sub] = self.codebooks[mi][arr[:, mi]]
        return out

    def query_vec(self, q: np.ndarray) -> np.ndarray:
        return np.asarray(q, dtype=np.float32)

    def scores(self, packed: np.ndarray, q: np.ndarray) -> np.ndarray:
        if self.codebooks is None:
            raise CodecError("PQ codec not trained")
        arr = np.atleast_2d(packed)
        q = np.asarray(q, dtype=np.float32)
        total = np.zeros(arr.shape[0], dtype=np.float32)
        for mi in range(self.m):
            qsub = q[mi * self.sub:(mi + 1) * self.sub]
            lut = self.codebooks[mi] @ qsub          # (ks,)
            total += lut[arr[:, mi]]
        return total

    def to_bytes(self, packed_row: np.ndarray) -> bytes:
        return packed_row.tobytes()

    def from_bytes(self, b: bytes) -> np.ndarray:
        return np.frombuffer(b, dtype=np.uint8).copy()

    def corrupt(self, packed_row: np.ndarray, rate: float,
                rng: np.random.Generator) -> np.ndarray:
        out = packed_row.copy()
        idx = rng.integers(0, len(out), size=max(1, int(len(out) * rate)))
        out[idx] = rng.integers(0, self.ks, size=len(idx)).astype(np.uint8)
        return out


def make_codec(name: str, dims: int, seed: int = 0x0C0FFEE,
               tmr: bool = False, **kw) -> BaseCodec:
    if name == "int8":
        return Int8Codec(dims)
    if name == "binary":
        return BinaryCodec(dims, tmr=tmr, seed=seed)
    if name == "rabitq":
        return RaBitQCodec(dims, seed=seed)
    if name == "pq":
        return PQCodec(dims, seed=seed, **kw)
    raise CodecError(f"unknown codec {name!r}")
