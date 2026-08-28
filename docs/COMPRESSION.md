# The Compression Stack — cortexm-compress

Per-vector storage at 768 dims across four tiers, with measured
quality trade-offs (20K realistic fact holograms, hologram probes;
see `benchmarks/results/micro.json` and `docs/BENCHMARKS.md`).

| Tier | Bytes/vector | 1M memories | Measured quality |
|---|---:|---:|---|
| `int8` | 770 | 770 MB | overlap@10 vs FP32 **0.90**, self-hit 1.00 |
| `binary` (+JL rotation, optional TMR) | 96 (288 w/ TMR) | 96 MB | recall@10 in top-50 **1.00**, self-hit 1.00 |
| `rabitq` | 96 | 96 MB | recall@10 in top-50 **1.00**, self-hit 1.00 |
| `pq` (M=8 × 8-bit) | 8 | 8 MB | recall@10 in top-50 **0.9995** |

## Reading the numbers honestly

`int8` is the near-lossless default — the workhorse. `binary`,
`rabitq` and `pq` are **shortlist codecs**: their raw top-10 membership
differs from FP32 (coarse Hamming/LUT scores on near-tied neighbors),
but they recover the full-precision top-10 inside their top-50 at
~1.00. That is exactly how an edge deployment should use them: coarse
neural shortlist → symbolic dereference → the Trace (exact triples,
temporal windows, contradiction chains) supplies precision. Our
benchmark pipeline itself demonstrates the pattern — the final ranking
fuses VSA scores with symbolic boosts, so approximate vectors + exact
graph = correct answers.

## Why binary gets a rotation

Sparse hashed embeddings binarize badly — near-zero components produce
correlated sign noise (unrelated facts scoring 0.8+ similarity). A
fixed JL rotation densifies energy before binarization (the RaBitQ
insight applied to the MAP model), which restores discrimination. Set
`rotate=False` on the codec for raw bipolar-MAP semantics.

## Self-healing (the "Proof of God" property)

Binary HDC tolerates bit flips far beyond what dense floats can: a
corrupted hypervector still self-identifies among 5,000 stored vectors
at **100% up to 10% corruption, 96% at 20%** (measured). Beyond the
correction radius, per-record hashes detect the damage and the palace
re-encodes the vector from the symbolic Trace — the source of truth
never lived in the vectors. With TMR (three copies, bit-level majority
vote, 288 B/vector) the correction radius extends further and
corruption is *detectable* in storage.

## The hardware roadmap

The `perm` VSA mode uses permutation binding and the binary codec uses
packed bits + XOR + popcount — the exact operation set of HDC
accelerators (ImageHD-class FPGAs: 383× energy efficiency; FSL-HDnn
single-cycle binary dot products). When edge ASICs arrive (2027-2028
per the brief), the same encode/score paths compile down; the codecs
were designed as the porting surface.

## Choosing a tier

- **Laptop / server, quality first** → `int8` (default)
- **Edge device** → `binary` (+`tmr=True` when storage is flaky)
- **Extreme compression** → `pq` (cloud, billions of vectors)
- `rabitq` → same footprint as binary when you want the rotation
  semantics under its own name

```python
from cortexm import Memory
from context_m.config import Config
m = Memory(Config(codec="binary", tmr=True))   # edge profile
```
