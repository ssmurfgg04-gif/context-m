# Context-M Architecture

The Universal Neuro-Symbolic Memory Fabric. Every claim below maps to
code you can read and run.

## Design thesis

Vector-only memory systems (Mem0-class) retrieve *similar text*; they
cannot answer "where did Alice work in 2025?" after she changed jobs,
cannot prove why a fact was retrieved, and burn an LLM call per write.
Context-M's thesis: a **deterministic symbolic Trace** for structure,
truth and time; a **VSA Memory Palace** for fuzzy recall; and a
**deterministic bridge** that never calls an LLM (μ=0). The two layers
cover each other's weaknesses, and the audit chain falls out for free.

## Layer 1 — the Symbolic Trace (`context_m/trace/`)

**Data model** (`fact.py`): `(subject, relation, value)` triples with
bi-temporal timestamps — `valid_from/valid_to` (when the fact was true
in reality) and `tx_from/tx_to` (when the system recorded it) — plus
confidence, BLAKE3 `source_hash`, scope (`user_id/agent_id/run_id`),
memory type, access/reinforcement counters, and active/quarantined/
derived flags.

**Storage** (`store.py`): embedded SQLite (WAL) with tables for facts,
edges (`CONTRADICTS`, `TEMPORALLY_PRECEDED_BY`, `EXTRACTED_FROM`),
source chunks, hash-chained commits, and branches. Ancestry-cached
active sets make branch-aware reads cheap. The store API is designed so
an ArcadeDB backend can replace SQLite behind the same interface (the
plan's production graph engine; KuzuDB is dead — acquired and archived
in late 2025).

**Truth maintenance** (`contradictions.py`): exact + fuzzy (Jaccard +
banded Levenshtein) matching on subject-relation pairs. Single-valued
relations (works_at, lives_in…) supersede: the old fact gets a
`valid_to`, a `CONTRADICTS` edge points to the new one, history stays
queryable. Multi-valued relations coexist. Near-duplicates merge and
reinforce instead of duplicating.

**Rules engine** (`rules.py`): Datalog-lite, forward chaining to a
fixpoint. Shipped rules: `reports_to(X,Y) :- manages(Y,X)`;
`team_uses(X,L) :- member_of(X,T), uses(T,L)`; `lives_in(X,C) :-
moved_to(X,C)`; `same_person(N,X) :- alias(X,N)`. Derived facts are
materialized into the palace — inference participates in retrieval.

**Lifecycle** (`lifecycle.py`): interference-aware. Before commitment a
candidate is evaluated against active memory (merge / supersede /
coexist / quarantine). Retention is `confidence × recency ×
reinforcement / contradiction-pressure` — not a pure Ebbinghaus decay —
and consolidation promotes reinforced short-term facts to long-term.

## Layer 2 — the VSA Memory Palace (`context_m/vsa/`)

**Algebra** (`ops.py`): each fact is encoded as
`normalize(bind(S, s) + bind(R, r) + bind(V, v) + λ·normalize(s+r+v))`.
Binding modes: `perm` (permutation — similarity-preserving, directly
portable to HDC hardware via XOR/permutation), `conv` (classic HRR
circular convolution), `bag` (ablation). The λ-term keeps free-text
queries effective while bound terms carry structure for probe queries;
`unbind_role()` recovers approximate fillers for the audit path.

**Codecs** (`codecs.py`): the cortexm-compress tier stack —
`int8` (770 B, near-lossless), `binary` (96 B, bipolar ±1 with a fixed
JL rotation — the RaBitQ insight applied to the MAP model — plus
optional TMR), `rabitq` (96 B), `pq` (8 B, ADC lookup tables). All
share one interface (`encode_packed / decoded / query_vec / scores /
corrupt`) so the palace and index are codec-agnostic.

**Index** (`index.py`): page-clustered tree. Recursive k-means
(branching 8), leaves ≤ 512 packed vectors, exact per-node radii,
best-first search over the bound `centroid_sim − radius` with exact
scoring inside visited leaves. p50 ≈ 0.4–1.1 ms at 10K–100K vectors.

**Palace** (`palace.py`): BLOB persistence + RAM packed matrices;
per-record hash for corruption detection; TMR majority vote; re-encode
from the Trace when corruption exceeds the correction radius.

**SLB** (`slb.py`): 64-entry semantic lookaside buffer — a query whose
signature is ≥0.97 cosine to a cached signature reuses the cached
ranking. Conversational follow-ups are near-duplicates; hits cost a
single 64×768 dot product (~6 µs).

## The Bridge (`context_m/bridge/`)

**Extraction** (`patterns.py`, `extractor.py`): ~60 high-precision
patterns across first-person, third-person (with pronoun resolution)
and assistant-turn forms; relative + absolute date resolution
(`dates.py`); retractions ("I left Google in January") that retire
stale facts; instruction capture; low-confidence entity-mention
fallbacks whose values dedupe by entity identity, so memory grows
sublinearly with conversation noise. A single trigger regex prefilters
sentences so distractor-heavy corpora ingest at 100K+ tokens/s.

**Writer** (`writer.py`): batched transactions (SQLite commit per
session, not per fact), InjecMEM screening, lifecycle application,
temporal edge wiring, deferred Datalog passes for bulk ingest.

**Reader** (`reader.py`): deterministic intent planner — temporal
windows (interval-overlap semantics), event-ordering proofs
(`ORDERING: X (2024-03-03) happened before Y (2024-06-20)`), counting,
supersession chains, current-state resolution, and two-round entity-hop
expansion for multi-hop questions. Fusion: VSA score (damped for
mention anchors) + symbolic boost (relation-hinted, entity-matched), a
per-relation diversity cap so one relation cannot flood the context
block, and prefetcher boosts. Output: the `[Memory — Known facts]`
context block plus the full provenance chain per fact.

## The five features (`context_m/features/`, `federation.py`)

- **Memory Git** — hash-chained commits form a DAG; branches fork
  memory state; 3-way merge (latest-wins or union) with conflict
  resolution through the lifecycle engine; diff compares active sets at
  two commits; blame walks a fact chain's commits.
- **ZK-lite proofs** — Merkle membership over the active-fact leaf set
  (BLAKE3 of `id:source_hash`) + HMAC attestation binding
  {statement, root, timestamp}. The LLM sees `[ZK-Proof: match on
  'allergy' verified. Content redacted.]`. Full SNARKs over the
  similarity predicate are the roadmap (binary-codec Hamming distance
  is a natural circuit).
- **Self-healing memory** — per-record hashes detect bit flips; TMR
  majority vote corrects within radius; beyond radius, re-encode from
  the Trace (source of truth) and re-verify.
- **Predictive prefetching (MBTB)** — co-access index learned from
  retrieval histories feeds score boosts; wrong predictions cost
  nothing.
- **Cross-modal episodic binding** — `VSA.probe/bind/unbind` compose
  arbitrary role-fillers (text/structured/sensor) into episodic
  holograms recallable from any modality.

## Surfaces

`context_m/api/memory.py` — the Mem0-compatible facade plus temporal
queries, audit, Memory Git, proofs, healing, federation.
`context_m/mcp/server.py` — dependency-free MCP stdio server (9 tools).
`context_m/cli.py` — `cortexm serve|stats|verify|consolidate|migrate|
cost|bench|export-schema|git`. `context_m/migrate/importers.py` —
mem0 / Zep / Chroma importers.

## Performance engineering notes

The war stories that got us to 104K tokens/s: (1) per-operation SQLite
commits → batched transactions (16×); (2) quadratic Levenshtein over
mention snippets → Jaccard fast paths + banded cutoffs + exact-dup
mention semantics; (3) per-message Datalog passes → deferred bulk pass;
(4) a single trigger prefilter before the 60-pattern battery; (5)
f-string quantifier braces are a regex graveyard — `{2,40}` in an
f-string is the tuple `(2, 40)`.
