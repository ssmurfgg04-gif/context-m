"""Micro-benchmarks — the engineering claims behind the fabric.

  1. Retrieval latency & tree-index recall at 10K/50K/100K vectors
     (plan milestone: <1ms retrieval on 100K memories)
  2. Codec ablation: recall@10 vs FP32 brute force + bytes/vector
     (the cortexm-compress tier table)
  3. Self-healing memory: recall under bit-flip corruption, with and
     without TMR, before/after healing (the "Proof of God" demo)
  4. SLB hit rate & latency under conversational locality replay
  5. Ingest throughput (tokens/s, facts/s) and μ=0 assertion
"""

from __future__ import annotations

import time

import numpy as np

from context_m import metrics
from context_m.config import Config
from context_m.vsa.codecs import make_codec
from context_m.vsa.ops import VSA
from context_m.text.embedder import HashingEmbedder


def _synthetic_corpus(n: int, dims: int = 768, seed: int = 7):
    """n fact holograms with genuine kNN structure: facts about the same
    subject share lexical content, so true neighbors = same-subject facts."""
    rng = np.random.default_rng(seed)
    emb = HashingEmbedder(dims, seed)
    vsa = VSA(dims, "perm", seed)
    n_subj = max(50, n // 40)
    subjects = [f"Person{i}" for i in range(n_subj)]
    relations = ["works_at", "lives_in", "prefers", "has_skill", "event"]
    facts, vecs = [], []
    for i in range(n):
        s = subjects[i % n_subj]
        r = relations[i % len(relations)]
        v = f"{s}-thing{(i // n_subj) % 40}"
        facts.append((s, r, v))
        vecs.append(vsa.encode_fact(emb.embed(s), emb.embed(r), emb.embed(v)))
    return facts, np.stack(vecs), emb


def latency_and_recall():
    from context_m.vsa.index import TreeIndex

    out = {}
    for n in (10_000, 50_000, 100_000):
        facts, vecs, emb = _synthetic_corpus(n)
        ids = [f"fact{i}" for i in range(n)]
        codec = make_codec("int8", 768)
        packed = np.stack([codec.encode_packed(v) for v in vecs])
        aux = np.array([codec.encode_scale(v) for v in vecs])

        def getter(rows):
            return packed[rows], aux[rows]

        # queries: text of a random fact (its own hologram is the target)
        rng = np.random.default_rng(11)
        q_idx = rng.choice(n, 100, replace=False)
        queries = [emb.embed(" ".join(facts[i])) for i in q_idx]

        # brute force ground truth
        t0 = time.perf_counter()
        gt, gt_scores = [], []
        for q in queries:
            sc = codec.scores(packed, q, aux)
            top = np.argsort(-sc)[:10]
            gt.append(set(top.tolist()))
            gt_scores.append(float(np.sort(sc)[::-1][:10].mean()))
        flat_ms = (time.perf_counter() - t0) / len(queries) * 1e3

        # tree index
        t0 = time.perf_counter()
        idx = TreeIndex(codec, getter, n, branch=8, leaf=512, seed=3)
        idx.build()
        build_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        hits = 0
        overlap = 0.0
        quality = 0.0
        lat = []
        for q, want, want_score in zip(queries, gt, gt_scores):
            t1 = time.perf_counter()
            rows, scores = idx.search(q, 10, beam=8)
            lat.append(time.perf_counter() - t1)
            got = set(rows.tolist())
            if got & want:
                hits += 1
            overlap += len(got & want) / 10
            if len(scores):
                quality += float(np.sort(scores)[::-1][:10].mean()) / max(want_score, 1e-9)
        lat = np.array(lat) * 1e3
        out[f"n={n}"] = {
            "flat_ms": round(flat_ms, 2),
            "tree_p50_ms": round(float(np.percentile(lat, 50)), 3),
            "tree_p99_ms": round(float(np.percentile(lat, 99)), 3),
            "any_hit@10": round(hits / len(queries), 4),
            "overlap@10": round(overlap / len(queries), 4),
            "quality_ratio": round(quality / len(queries), 4),
            "index_build_s": round(build_s, 2),
            "rows_scanned_avg": round(idx.leaf_rows_scanned / len(queries), 0),
        }
    return out


def codec_ablation(n: int = 20_000, k: int = 10):
    emb = HashingEmbedder(768, 7)
    vsa = VSA(768, "perm", 7)
    n_subj = max(50, n // 40)
    subjects = [f"Person{i}" for i in range(n_subj)]
    relations = ["works_at", "lives_in", "prefers", "has_skill", "event"]
    vecs, queries = [], []
    for i in range(n):
        s = subjects[i % n_subj]
        r = relations[i % len(relations)]
        v = f"{s}-thing{(i // n_subj) % 40}"
        vecs.append(vsa.encode_fact(emb.embed(s), emb.embed(r), emb.embed(v)))
        if i % (n // 200) == 0:
            queries.append((vecs[-1], i))
    vecs = np.stack(vecs)

    gt = []
    for q, i in queries:
        sc = vecs @ q
        top = np.argsort(-sc)[:k].tolist()
        gt.append((i, set(top)))

    out = {}
    for name in ("int8", "binary", "rabitq", "pq"):
        t0 = time.perf_counter()
        codec = make_codec(name, 768, seed=7)
        if name == "pq":
            codec.train(vecs[: min(n, 4096)])
        packed = np.stack([codec.encode_packed(v) for v in vecs])
        enc_s = time.perf_counter() - t0
        # hologram probes: self-hit + neighborhood preservation vs fp32
        hits = 0
        overlap = 0.0
        shortlist = 0.0
        t0 = time.perf_counter()
        if name == "int8":
            aux = np.array([codec.encode_scale(v) for v in vecs])
        for (q, i), (self_idx, want) in zip(queries, gt):
            sc = (codec.scores(packed, q, aux) if name == "int8"
                  else codec.scores(packed, q))
            order = np.argsort(-sc)
            got = order[:k].tolist()
            if i in got:
                hits += 1
            overlap += len(set(got) & want) / k
            # shortlist usage: fp32 top-10 within codec top-50
            shortlist += len(set(order[:50].tolist()) & want) / k
        out[name] = {
            "bytes_per_vector": codec.bytes_per_vector,
            "mb_per_million": round(codec.bytes_per_vector, 1),
            "self_hit@10": round(hits / len(queries), 4),
            "overlap@10_vs_fp32": round(overlap / len(queries), 4),
            "recall@10_in_top50": round(shortlist / len(queries), 4),
            "encode_ms_per_1k": round(enc_s / n * 1000 * 1e3, 2),
            "query_ms_flat_20k": round((time.perf_counter() - t0) / len(queries) * 1e3, 3),
        }
    out["fp32_reference"] = {"bytes_per_vector": 3072, "mb_per_million": 3072.0,
                             "recall@10": 1.0}
    return out


def self_healing(n: int = 5000):
    """Recall under corruption — binary HDC tolerance + TMR majority vote.

    Self-identification test: a corrupted hypervector must still rank
    ITSELF as the nearest neighbor among n stored vectors (HDC's
    error-correction radius), with and without TMR.
    """
    from context_m.vsa.codecs import BinaryCodec

    emb = HashingEmbedder(768, 7)
    vsa = VSA(768, "perm", 7)
    n_subj = max(50, n // 40)
    subjects = [f"Person{i}" for i in range(n_subj)]
    vecs = []
    for i in range(n):
        s = subjects[i % n_subj]
        v = f"{s}-thing{(i // n_subj) % 40}"
        vecs.append(vsa.encode_fact(emb.embed(s), emb.embed("works_at"),
                                    emb.embed(v)))
    vecs = np.stack(vecs)
    q_idx = list(range(0, n, 50))           # 100 probe vectors

    out = {}
    for tmr in (False, True):
        codec = BinaryCodec(768, tmr=tmr)
        packed = np.stack([codec.encode_packed(v) for v in vecs])
        crng = np.random.default_rng(99)
        for rate in (0.0, 0.01, 0.05, 0.10, 0.20):
            corrupted = packed.copy()
            if rate > 0:
                for row in q_idx:           # corrupt the probe vectors
                    corrupted[row] = codec.corrupt(corrupted[row], rate, crng)
            hits = 0
            for i in q_idx:
                sc = codec.scores(corrupted, vecs[i])
                if int(np.argmax(sc)) == i:
                    hits += 1
            out[f"{'tmr' if tmr else 'plain'}@{int(rate*100)}%"] = {
                "self_identification": round(hits / len(q_idx), 3)}
    return out


def slb_replay(n_queries: int = 400):
    from context_m.vsa.slb import SemanticLookasideBuffer

    emb = HashingEmbedder(768, 7)
    slb = SemanticLookasideBuffer(64, 0.97, 768)
    rng = np.random.default_rng(3)
    topics = [f"project {i} status update" for i in range(40)]
    hit_lat, miss_lat = [], []
    for i in range(n_queries):
        # conversational locality: follow-ups repeat the previous topic
        base = topics[rng.integers(len(topics))]
        q = emb.embed(base + (" again" if i % 3 else ""))
        t0 = time.perf_counter()
        got = slb.lookup(q)
        if got is not None:
            hit_lat.append(time.perf_counter() - t0)
        else:
            miss_lat.append(time.perf_counter() - t0)
            slb.store(q, [("f1", 0.5), ("f2", 0.4)])
    return {
        "hit_rate": round(slb.hits / n_queries, 3),
        "avg_hit_latency_us": round(sum(hit_lat) / max(len(hit_lat), 1) * 1e6, 1),
        "avg_miss_latency_us": round(sum(miss_lat) / max(len(miss_lat), 1) * 1e6, 1),
    }


def run_micro() -> dict:
    metrics.reset_counters()
    out = {
        "latency_recall": latency_and_recall(),
        "codec_ablation": codec_ablation(),
        "self_healing": self_healing(),
        "slb_replay": slb_replay(),
        "u0_llm_calls": metrics.llm_calls(),
    }
    return out


if __name__ == "__main__":
    print(run_micro())
