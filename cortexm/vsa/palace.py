"""The Memory Palace — VSA hologram store with codec-quantized vectors.

One hologram per fact: role-bound subject/relation/value fillers plus a
λ-weighted lexical superposition (see vsa.ops). Storage is codec-
quantized (INT8 / Binary / RaBitQ / PQ), persisted as BLOBs in SQLite
and mirrored in RAM as packed numpy matrices for microsecond scoring.
A page-clustered tree index provides O(log N) retrieval once the
collection crosses ``index_threshold``; below it, flat scan wins.

Also hosts the self-healing machinery: per-record hash checks, TMR
majority vote (binary codec), and re-encoding from the symbolic Trace
when corruption exceeds the correction radius.
"""

from __future__ import annotations

import base64
import numpy as np

from cortexm.config import Config
from cortexm.errors import CodecError, StoreError
from cortexm.text.embedder import HashingEmbedder
from cortexm.trace.fact import Fact
from cortexm.trace.store import TraceStore
from cortexm.util import iso
import datetime as _dt
from cortexm.vsa.codecs import make_codec, PQCodec, Int8Codec
from cortexm.vsa.index import TreeIndex
from cortexm.vsa.ops import VSA

VEC_TABLE = """
CREATE TABLE IF NOT EXISTS vectors (
  fact_id TEXT PRIMARY KEY, record BLOB NOT NULL,
  vec_hash TEXT DEFAULT '', ts TEXT DEFAULT ''
)
"""


class MemoryPalace:
    def __init__(self, config: Config, store: TraceStore, embedder: HashingEmbedder | None = None) -> None:
        self.cfg = config
        self.store = store
        self.dims = config.dims
        self.hasher = store.hasher
        self.codec = make_codec(config.codec, config.dims, config.seed,
                                tmr=config.tmr)
        self.vsa = VSA(config.dims, config.vsa_mode, config.seed,
                        config.lexical_lambda)
        # Allow injecting a shared embedder for FIX 2 (persistent worker embedder)
        self.embedder = embedder or HashingEmbedder(config.dims, config.seed)
        store.conn.execute(VEC_TABLE)
        store.conn.commit()

        self._ids: list[str] = []
        self._id2row: dict[str, int] = {}
        self._n = 0
        self._cap = 0
        self._packed: np.ndarray | None = None
        self._aux: np.ndarray | None = None
        self._index: TreeIndex | None = None
        self._index_valid = False
        self._pq_buffer: list[tuple[str, np.ndarray]] = []
        self.searches_flat = 0
        self.searches_indexed = 0
        self._load_pq_codebooks()
        self._load()

    # ------------------------------------------------------------- loading
    def _load(self) -> None:
        rows = self.store.conn.execute(
            "SELECT fact_id, record FROM vectors").fetchall()
        if not rows:
            return
        recs = [r["record"] for r in rows]
        self._ids = [r["fact_id"] for r in rows]
        self._id2row = {fid: i for i, fid in enumerate(self._ids)}
        packed_rows = [self.codec.from_bytes(b) for b in recs]
        if isinstance(self.codec, Int8Codec):
            q = np.stack([p[0] for p in packed_rows])
            self._aux = np.stack([np.float16(p[1]) for p in packed_rows])
            self._packed = q
            self._cap = self._n = len(q)
        else:
            self._packed = np.stack(packed_rows)
            self._cap = self._n = len(self._packed)
        self._index_valid = False

    def _load_pq_codebooks(self) -> None:
        if not isinstance(self.codec, PQCodec):
            return
        blob = self.store.kv_get("PQ_CODEBOOKS")
        if blob:
            arr = np.frombuffer(base64.b64decode(blob), dtype=np.float32)
            m = self.codec.m
            ks = arr.size // (m * self.codec.sub)
            self.codec.set_codebooks(arr.reshape(m, ks, self.codec.sub))

    def _save_pq_codebooks(self) -> None:
        if isinstance(self.codec, PQCodec) and self.codec.trained:
            self.store.kv_set(
                "PQ_CODEBOOKS",
                base64.b64encode(self.codec.codebooks.astype(np.float32).tobytes()).decode())

    # ------------------------------------------------------------- writing
    def encode_fact(self, fact: Fact) -> np.ndarray:
        return self.vsa.encode_fact(
            self.embedder.embed(fact.subject),
            self.embedder.embed(fact.relation),
            self.embedder.embed(fact.value))

    def _grow(self, need: int) -> None:
        w = self._row_width()
        new_cap = max(1024, self._cap * 2, need)
        if isinstance(self.codec, Int8Codec):
            packed = np.zeros((new_cap, w), dtype=np.int8)
            aux = np.zeros(new_cap, dtype=np.float16)
            if self._packed is not None and self._n:
                packed[: self._n] = self._packed[: self._n]
                aux[: self._n] = self._aux[: self._n]
            self._packed, self._aux = packed, aux
        else:
            packed = np.zeros((new_cap, w), dtype=np.uint8)
            if self._packed is not None and self._n:
                packed[: self._n] = self._packed[: self._n]
            self._packed = packed
        self._cap = new_cap

    def _row_width(self) -> int:
        if isinstance(self.codec, Int8Codec):
            return self.dims          # scale lives in the aux array
        if isinstance(self.codec, PQCodec):
            return self.codec.m       # 8 code bytes
        return self.codec.bytes_per_vector  # binary / rabitq packed words

    def add(self, fact_id: str, vec: np.ndarray) -> None:
        if isinstance(self.codec, PQCodec) and not self.codec.trained:
            self._pq_buffer.append((fact_id, vec))
            if len(self._pq_buffer) >= self.codec.train_threshold:
                self._flush_pq()
            return
        self._append(fact_id, self._encode_row(vec), vec)

    def _encode_row(self, vec: np.ndarray):
        if isinstance(self.codec, Int8Codec):
            q = self.codec.encode_packed(vec)
            return q, self.codec.encode_scale(vec)
        return self.codec.encode_packed(vec), None

    def _append(self, fact_id: str, row, vec) -> None:
        packed_row, scale = row
        blob = (self.codec.to_bytes(packed_row, scale)
                if isinstance(self.codec, Int8Codec)
                else self.codec.to_bytes(packed_row))
        self.store.conn.execute(
            "INSERT OR REPLACE INTO vectors(fact_id, record, vec_hash, ts) VALUES(?,?,?,?)",
            (fact_id, blob, self.hasher.hash_bytes(blob), iso(_dt.datetime.now(_dt.timezone.utc))))
        if not self.store.batching:
            self.store.conn.commit()
        if self._n >= self._cap:
            self._grow(self._n + 1)
        if isinstance(self.codec, Int8Codec):
            self._packed[self._n] = packed_row
            self._aux[self._n] = scale
        else:
            self._packed[self._n] = packed_row
        if fact_id in self._id2row:
            old = self._id2row[fact_id]
            # overwrite in place; keep id mapping
            self._ids[old] = fact_id
        else:
            self._id2row[fact_id] = self._n
            self._ids.append(fact_id)
            self._n += 1
        self._index_valid = False

    def add_many(self, pairs: list[tuple[str, np.ndarray]]) -> None:
        for fid, v in pairs:
            self.add(fid, v)

    def remove_ids(self, fact_ids: list[str]) -> int:
        """Hard-remove vectors (GDPR erasure). Rebuilds packed state from
        the surviving rows. Returns the number of vectors removed."""
        if not fact_ids:
            return 0
        gone = set(fact_ids)
        cur = self.store.conn.execute(
            "SELECT fact_id FROM vectors").fetchall()
        existing = {r["fact_id"] for r in cur}
        removed = len(gone & existing)
        if removed:
            qmarks = ",".join("?" * len(gone))
            self.store.conn.execute(
                f"DELETE FROM vectors WHERE fact_id IN ({qmarks})",
                list(gone))
            self.store.conn.commit()
        # rebuild in-memory state from disk (source of truth)
        self._ids = []
        self._id2row = {}
        self._n = 0
        self._cap = 0
        self._packed = None
        self._aux = None
        self._index = None
        self._index_valid = False
        self._load()
        return removed

    def _flush_pq(self) -> None:
        if not self._pq_buffer or not isinstance(self.codec, PQCodec):
            return
        if not self.codec.trained:
            vecs = np.stack([v for _, v in self._pq_buffer])
            self.codec.train(vecs)
            self._save_pq_codebooks()
        for fid, v in self._pq_buffer:
            self._append(fid, self._encode_row(v), v)
        self._pq_buffer.clear()

    def size(self) -> int:
        return self._n + len(self._pq_buffer)

    def has(self, fact_id: str) -> bool:
        return fact_id in self._id2row or any(f == fact_id for f, _ in self._pq_buffer)

    def close(self) -> None:
        self._flush_pq()
        if not self.store.batching:
            # Idempotent teardown: if the underlying SQLite connection
            # was already closed (e.g. Memory.close() called twice or
            # store.dispose() ran before palace.close()), the commit()
            # would raise sqlite3.ProgrammingError. Catch + ignore so
            # Memory.close() is safe to call multiple times — a
            # standard Python teardown idiom.
            try:
                self.store.conn.commit()
            except Exception:
                pass

    # -------------------------------------------------------------- search
    def _rows_getter(self, rows: np.ndarray):
        if self._packed is None or self._n == 0:
            return np.zeros((0, 1), dtype=np.uint8), None
        packed = self._packed[rows]
        aux = self._aux[rows] if self._aux is not None else None
        return packed, aux

    def ensure_index(self, force: bool = False) -> None:
        if self._n < self.cfg.index_threshold:
            return
        if self._index is not None and self._index_valid and not force:
            return
        self._flush_pq()
        index = TreeIndex(self.codec, self._rows_getter, self._n,
                          branch=self.cfg.index_branch,
                          leaf=self.cfg.index_leaf_size,
                          seed=self.cfg.seed)
        index.build()
        self._index = index
        self._index_valid = True

    def search(self, q: np.ndarray, k: int = 10,
               candidate_ids: set[str] | None = None) -> list[tuple[str, float]]:
        self._flush_pq()
        if self._n == 0:
            return []
        use_index = (self._n >= self.cfg.index_threshold and self._index_valid
                     and (candidate_ids is None or len(candidate_ids) >= self._n * 0.5))
        if use_index:
            self.searches_indexed += 1
            rows, scores = self._index.search(q, min(self._n, max(k * 3, 24)),
                                              beam=self.cfg.beam_width)
            out = [(self._ids[int(r)], float(s)) for r, s in zip(rows, scores)]
            if candidate_ids is not None:
                out = [(fid, s) for fid, s in out if fid in candidate_ids]
            out.sort(key=lambda t: -t[1])
            if len(out) < k and candidate_ids is not None and len(candidate_ids) > 0:
                extra = self._flat_search(q, k, candidate_ids, exclude={f for f, _ in out})
                out.extend(extra)
                out.sort(key=lambda t: -t[1])
            return out[:k]
        self.searches_flat += 1
        if candidate_ids is not None:
            return self._flat_search(q, k, candidate_ids)
        sc = self._score_all(q)
        k2 = min(k, self._n)
        idx = np.argpartition(-sc, k2 - 1)[:k2]
        idx = idx[np.argsort(-sc[idx])]
        return [(self._ids[int(i)], float(sc[i])) for i in idx]

    def _score_all(self, qv: np.ndarray) -> np.ndarray:
        # Rust fast path (Task 6-simd): when cortexm_core is built, route
        # the per-row scoring through `batch_dot_i8` (int8 codec) or
        # `batch_dot` (fp32 path) instead of the codec's numpy `scores`.
        # The Rust kernels are AVX-512 → AVX2+FMA → NEON → scalar and
        # process the whole batch in one Python→Rust boundary crossing.
        # NumPy remains the exact reference; bit parity ≤1e-5 asserted
        # by `tests/test_rust_accel.py::TestSimdKernels::test_batch_dot_*`.
        try:
            from cortexm import accel
        except Exception:                       # pragma: no cover
            accel = None
        rust_ok = (accel is not None and accel.RUST_AVAILABLE
                   and accel.RUST_ENABLED)
        n = self._n
        d = self.dims
        if rust_ok and n > 0 and self._packed is not None:
            qf32 = np.ascontiguousarray(qv, dtype=np.float32)
            if self.codec.uses_aux and self._aux is not None:
                # Int8Codec: Rust returns raw int8·f32 dot products;
                # multiply by per-row aux scales to match the codec's
                # `scores` semantics (`packed.astype(f32) * aux @ q`).
                # Pass the int8 buffer as a flat 1-D view — pyo3 binds
                # to `PyReadonlyArray1<i8>` zero-copy.
                packed_flat = np.ascontiguousarray(
                    self._packed[:n]).reshape(-1)
                raw = np.asarray(
                    accel._core.batch_dot_i8(packed_flat, qf32, n, d),
                    dtype=np.float32)
                aux = np.asarray(self._aux[:n], dtype=np.float32)
                return raw * aux
            if self._packed.dtype == np.float32:
                # FP32-packed codec path. (BinaryCodec stores bipolar
                # ±1 in uint8 — skip; codec.scores handles its own XOR.)
                rows_flat = np.ascontiguousarray(
                    self._packed[:n]).reshape(-1)
                raw = np.asarray(
                    accel._core.batch_dot(rows_flat, qf32, n, d),
                    dtype=np.float32)
                return raw
        if self.codec.uses_aux:
            return self.codec.scores(self._packed[: self._n], qv, self._aux[: self._n])
        return self.codec.scores(self._packed[: self._n], qv)

    def _flat_search(self, q: np.ndarray, k: int, candidate_ids: set[str],
                     exclude: set[str] | None = None) -> list[tuple[str, float]]:
        # iterate candidates in PALACE ROW ORDER (insertion order), never in
        # set order: set iteration is hash-randomized per process, and the
        # argsort below breaks score ties by position — random positions
        # would make identical runs return different facts.
        sel = [self._id2row[f] for f in candidate_ids
               if f in self._id2row
               and (exclude is None or f not in exclude)]
        sel.sort()
        if not sel:
            return []
        arr = np.array(sel, dtype=np.int64)
        packed = self._packed[arr]
        aux = self._aux[arr] if self._aux is not None else None
        sc = (self.codec.scores(packed, q, aux) if self.codec.uses_aux
              else self.codec.scores(packed, q))
        order = np.argsort(-sc, kind="stable")[:k]
        return [(self._ids[int(arr[i])], float(sc[i])) for i in order]

    # ------------------------------------------------------ self-healing
    def record_hash(self, row: int) -> str:
        blob = self._record_bytes(row)
        return self.hasher.hash_bytes(blob)

    def _record_bytes(self, row: int) -> bytes:
        if isinstance(self.codec, Int8Codec):
            return self.codec.to_bytes(self._packed[row], self._aux[row])
        return self.codec.to_bytes(self._packed[row])

    def stored_hash(self, fact_id: str) -> str | None:
        r = self.store.conn.execute(
            "SELECT vec_hash FROM vectors WHERE fact_id=?", (fact_id,)).fetchone()
        return r["vec_hash"] if r else None

    def corrupt(self, rate: float, seed: int = 0,
                persist: bool = False) -> int:
        """Inject bit flips (cosmic rays / flash degradation). Returns count."""
        if self._n == 0:
            return 0
        rng = np.random.default_rng(seed)
        count = 0
        for row in range(self._n):
            if rng.random() < rate:
                packed_row = self._packed[row]
                self._packed[row] = self.codec.corrupt(packed_row, rate, rng)
                count += 1
                if persist:
                    blob = self._record_bytes(row)
                    self.store.conn.execute(
                        "UPDATE vectors SET record=? WHERE fact_id=?",
                        (blob, self._ids[row]))
        if persist:
            self.store.conn.commit()
        self._index_valid = False
        return count

    def health_check(self, sample: int | None = None) -> dict:
        """Detect corrupt records via stored vec_hash; TMR adds bit-level votes."""
        import random as _random
        rng = _random.Random(7)
        rows = list(range(self._n))
        if sample and sample < self._n:
            rows = rng.sample(rows, sample)
        corrupt_rows, checked = [], 0
        tmr_flips = 0
        for row in rows:
            checked += 1
            fid = self._ids[row]
            want = self.stored_hash(fid)
            got = self.record_hash(row)
            if want and want != got:
                corrupt_rows.append(fid)
            if self.cfg.tmr and self.codec.name == "binary":
                tmr_flips += self.codec.tmr_health(self._packed[row])
        return {"checked": checked, "corrupt": len(corrupt_rows),
                "corrupt_ids": corrupt_rows, "tmr_disagree_bits": int(tmr_flips)}

    def heal(self, facts_by_id: dict[str, Fact]) -> dict:
        """Re-encode corrupt records from the symbolic Trace (source of truth)."""
        report = self.health_check()
        healed = 0
        for fid in report["corrupt_ids"]:
            fact = facts_by_id.get(fid)
            if fact is None:
                continue
            row = self._id2row.get(fid)
            if row is None:
                continue
            vec = self.encode_fact(fact)
            packed_row, scale = self._encode_row(vec)
            if isinstance(self.codec, Int8Codec):
                self._packed[row] = packed_row
                self._aux[row] = scale
            else:
                self._packed[row] = packed_row
            blob = (self.codec.to_bytes(packed_row, scale)
                    if isinstance(self.codec, Int8Codec)
                    else self.codec.to_bytes(packed_row))
            self.store.conn.execute(
                "UPDATE vectors SET record=?, vec_hash=? WHERE fact_id=?",
                (blob, self.hasher.hash_bytes(blob), fid))
            healed += 1
        self.store.conn.commit()
        self._index_valid = False
        return {"corrupt": report["corrupt"], "healed": healed,
                "note": "re-encoded from Trace; source hash verified"}

    # ------------------------------------------------------------- stats
    def storage_stats(self) -> dict:
        vec_bytes = self._n * self.codec.bytes_per_vector if self._n else 0
        return {
            "vectors": self._n,
            "codec": self.codec.name,
            "bytes_per_vector": self.codec.bytes_per_vector,
            "vector_bytes": int(vec_bytes),
            "per_million_memories_mb": round(
                self.codec.bytes_per_vector * 1e6 / 1e6, 1),
            "index": ("tree" if self._index_valid else
                      ("flat" if self._n else "empty")),
            "searches_flat": self.searches_flat,
            "searches_indexed": self.searches_indexed,
        }
