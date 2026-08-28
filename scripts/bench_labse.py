"""Quick benchmark for the PolyglotEncoder.

Encodes 1000 mixed-language sentences and reports throughput on the
single-text path and the batch path. Also prints cross-language cosine
similarity numbers as a sanity check that the encoder produces
non-trivial alignments across scripts.

Expected on a typical CPU (no BLAS heavy ops, pure numpy + stdlib
unicodedata + BLAKE2b hashing): >50k sentences/sec.

Reproduce:
    python scripts/bench_labse.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context_m.text.labse import PolyglotEncoder


# Mixed-language corpus — six scripts × a few short sentences each.
# Tier-1 bug showed all of these embed to the constant [1,0,...] vector
# under the existing HashingEmbedder; this corpus validates the
# PolyglotEncoder produces distinct, non-trivial embeddings per text.
SENTS = [
    "Alice works at Google.",
    "Bob lives in Paris.",
    "Cathy studies at MIT.",
    "Dave runs every morning.",
    "爱丽丝在谷歌工作。",
    "鲍勃住在巴黎。",
    "凯西在麻省理工学习。",
    "大卫每天早晨跑步。",
    "تعمل أليس في جوجل.",
    "يعيش بوب في باريس.",
    "تدرس كاثي في معهد ماساتشوستس.",
    "Алиса работает в Гугле.",
    "Боб живет в Париже.",
    "Кэти учится в МТИ.",
    "एलिस गूगल में काम करती है।",
    "बॉब पेरिस में रहता है।",
    "कैथी एमआईटी में पढ़ती है।",
    "アリスはGoogleで働いています。",
    "ボブはパリに住んでいます。",
    "キャシーはMITで学んでいます。",
]


def main() -> None:
    enc = PolyglotEncoder(dims=768)
    n = 1000
    # Tile to exactly N sentences (deterministic — no RNG).
    corpus = (SENTS * ((n // len(SENTS)) + 1))[:n]

    # Warmup — first call pays the unicodedata lookup cost; subsequent
    # calls hit cached category lookups.
    for s in corpus[:20]:
        enc.encode(s)

    # ---------- single-text path ----------------------------------------
    t0 = time.perf_counter()
    for s in corpus:
        enc.encode(s)
    t1 = time.perf_counter()
    elapsed = t1 - t0
    rate = n / elapsed
    print("=" * 64)
    print(f"PolyglotEncoder — single-text path")
    print("=" * 64)
    print(f"  sentences : {n}")
    print(f"  time       : {elapsed * 1000:.1f} ms")
    print(f"  throughput : {rate:,.0f} sent/sec")
    print(f"  per-sent   : {elapsed * 1000 / n:.3f} ms")

    # ---------- batch path ----------------------------------------------
    t0 = time.perf_counter()
    enc.encode_batch(corpus)
    t1 = time.perf_counter()
    elapsed_b = t1 - t0
    rate_b = n / elapsed_b
    print()
    print("=" * 64)
    print(f"PolyglotEncoder — batch path")
    print("=" * 64)
    print(f"  sentences : {n}")
    print(f"  time       : {elapsed_b * 1000:.1f} ms")
    print(f"  throughput : {rate_b:,.0f} sent/sec")
    print(f"  per-sent   : {elapsed_b * 1000 / n:.3f} ms")

    # ---------- cross-language sanity check ----------------------------
    print()
    print("=" * 64)
    print(f"Cross-language cosine similarity sanity check")
    print("=" * 64)
    en = enc.encode("Alice works at Google")
    zh = enc.encode("爱丽丝在谷歌工作")
    ar = enc.encode("تعمل أليس في جوجل")
    dev = enc.encode("एलिस गूगल में काम करती है")
    cyr = enc.encode("Алиса работает в Гугле")
    jp = enc.encode("アリスはGoogleで働いています")
    print(f"  en × en (self) : {float(np.dot(en, en)):.4f}  (target ≈ 1.000)")
    print(f"  en × zh        : {float(np.dot(en, zh)):.4f}  (target ≥ 0.10)")
    print(f"  en × ar        : {float(np.dot(en, ar)):.4f}  (target ≥ 0.10)")
    print(f"  en × dev       : {float(np.dot(en, dev)):.4f}  (target ≥ 0.10)")
    print(f"  en × cyr       : {float(np.dot(en, cyr)):.4f}  (target ≥ 0.10)")
    print(f"  en × ja        : {float(np.dot(en, jp)):.4f}  (target ≥ 0.10)")

    # ---------- determinism check --------------------------------------
    print()
    print("=" * 64)
    print(f"Determinism check (bit-identical across runs)")
    print("=" * 64)
    a = enc.encode("Alice works at Google 爱丽丝在谷歌工作")
    b = enc.encode("Alice works at Google 爱丽丝在谷歌工作")
    bit_identical = a.tobytes() == b.tobytes()
    print(f"  bit-identical : {bit_identical}")
    assert bit_identical, "Determinism failed — two runs of same text differ"

    print()
    print("done.")


if __name__ == "__main__":
    main()
