# Security Model — Provenance, InjecMEM Defense, Memory Git

## The threat landscape

The research brief names the August-2026 attack class **InjecMEM**:
a single crafted interaction can poison an agent's memory — no
read/edit access to the store required. Against that, a memory system
must answer three questions: *what do I know*, *why do I believe it*,
and *can I prove it*. Context-M answers all three deterministically.

## 1. Cryptographic provenance (the audit chain)

Every chunk is hashed at ingest (BLAKE3-256; BLAKE2b-256 fallback,
always disclosed). Every fact carries its `source_hash`. On retrieval,
the reader re-hashes the source chunk and compares — a tampered store
is detectable, and every returned fact ships with:

```
query → VSA match (score) → symbolic dereference (fact id) →
BLAKE3 source hash (verified ✓) → original source text
```

`Memory.audit(query)` returns this chain; `Memory.verify_integrity()`
audits the whole store (chunk hashes + vector record hashes);
`cortexm verify` runs it from the CLI. Measured: 100% provenance
completeness across all benchmark buckets, ~6 ms per audited
retrieval.

## 2. InjecMEM quarantine (`context_m/security/injection.py`)

High-risk patterns — "ignore all previous instructions", system-prompt
exfiltration probes, identity overrides, jailbreaks, credential
capture — quarantine every fact extracted from the offending message:
stored and hash-chained for forensics, but `is_active=0` and never
retrievable into prompt context. Medium-risk patterns (behavior
injections like "always respond with…") commit with an audit flag.
Negations ("never ignore…") are exempted. Quarantine counts surface in
`Memory.stats()`.

## 3. Second-order defense: the MINJA contagion guard

**MINJA** (arXiv:2503.03704) demonstrated that an attacker does not need
write access to the memory bank at all: craft a query whose *retrieved*
answer gets written back by the agent itself, and the poison enters
through the front door. The first-order pattern quarantine above misses
exactly this case — the write-back is a paraphrase or a lightly-edited
quote that defeats every regex.

Countermeasure (`contagion_scan` in `context_m/security/injection.py`):
quarantined source text is treated as a **tainted corpus**. Every ingest
is compared against it sentence-by-sentence:

- **verbatim-quote shortcut** — a quarantined sentence (≥25 chars)
  appearing inside new text quarantines immediately (the classic
  MINJA write-back);
- **token-overlap fallback** — sentence-level token Jaccard ≥ 0.50
  catches light edits that break the regex patterns (comma insertion,
  reordering, punctuation changes) while keeping ≥50% shared content.

Both checks are pure set arithmetic — the write path stays μ=0 and the
guard adds no measurable ingest latency (benchmarks re-verified after
the change). Benign text mentioning a quarantined *topic* stays clear:
common-word overlap bottoms out around 0.25–0.33 Jaccard, well under the
threshold. **Documented limitation:** deep paraphrase laundering (an LLM
rewriting poison in entirely different words) is out of scope for any
token-overlap defense; the provenance chain and Memory Git remain the
forensic backstop.

Config: `quarantine_contagion` (on by default), `contagion_threshold`
(default 0.50). Tests: `test_contagion_scan_unit`,
`test_minja_contagion_end_to_end` in `tests/test_fabric.py`.

## 4. Scope sandboxing

Facts are scoped to `(user_id, agent_id, run_id)`. Retrieval filters by
scope before fusion — agent-scoped facts cannot leak into user-scoped
retrievals, and cross-scope promotion is an explicit API action, not
an emergent behavior. The Semantic Lookaside Buffer is scope-keyed as
well: a cache hit can never serve one user's results for another user's
near-duplicate query.

## 5. Memory Git as forensic infrastructure

Every write batch is a commit whose chain hash covers its parents — a
tamper-evident history. `blame(subject, relation)` answers "which
commit introduced this fact, when, from what message"; `diff` shows
exactly what changed between any two points; branches allow isolating
an experiment (or quarantining a poisoning attempt) without touching
main. Enterprise rollback = checkout.

## 6. ZK-lite proofs (`context_m/features/zk.py`)

For contexts where content must not reach the LLM (patient records,
financial positions): `Memory.prove(query)` returns a Merkle membership
proof over the active-fact leaf set plus an HMAC attestation — the LLM
receives `[ZK-Proof: high-confidence match on 'allergy' verified.
Content redacted.]`. Scope note: this is commit-and-prove membership +
attestation; full ZK-SNARKs over the similarity predicate are the
roadmap, and the binary codec's Hamming-distance similarity is the
natural circuit candidate (HRR's circular convolution is a group
operation — the algebraic property standard cosine similarity cannot
satisfy, and the reason this feature cannot be bolted onto a vector
DB).

## 7. Defense-in-depth summary

| Layer | Mechanism | Defeats |
|---|---|---|
| Ingest | pattern quarantine + confidence floor | prompt-style memory poisoning |
| Ingest | MINJA contagion guard (taint corpus) | second-order re-ingestion of poison |
| Storage | BLAKE3 per fact/chunk/vector | silent tampering, corruption |
| Retrieval | hash re-verification, provenance chain | unverifiable answers |
| Structure | bi-temporal windows, CONTRADICTS edges | stale-fact hallucination |
| History | hash-chained commits, blame | "who wrote this and when" disputes |
| Privacy | ZK-lite proofs, scope sandboxing | content leakage to LLMs |
