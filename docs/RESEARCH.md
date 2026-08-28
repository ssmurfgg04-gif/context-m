# Context-M — Research Lineage

Every design decision in Context-M is grounded in published research. This
document maps the papers (surveyed via live arXiv search, August 2026) to
what Context-M adopted, adapted, or deliberately rejected — so that a
reviewer can audit the system against its sources instead of taking our
word for it.

## Adopted directly

| Paper | Core idea | Where it lives in Context-M |
|---|---|---|
| **BEAM** — arXiv:2510.27246 | 10-ability taxonomy for long-horizon agent memory (10M-token conversations); current SOTA ≈ 68% with LLM-in-loop systems | The benchmark harness (`cortexm/bench/`) implements the 10 abilities with seeded synthetic corpora at 128K/500K/1M/10M buckets; deterministic nugget judge with a pluggable LLM-judge slot for canonical replication |
| **Zep** — arXiv:2501.13956 | Bi-temporal knowledge graph for agent memory: valid time vs transaction time | The entire Trace layer: every fact carries `(valid_from, valid_to)` (when it was true in the world) *and* `(tx_from, tx_to)` (when the system knew it). `get_between/before/after` expose Zep-style temporal queries |
| **HippoRAG / HippoRAG 2** — arXiv:2505.14832 | Hippocampal memory indexing: knowledge graph + **Personalized PageRank** spreading activation for associative retrieval | The reader's entity-hop expansion (`_expand`, 2 rounds over the fact graph) is bounded-depth spreading activation; full PPR is the documented upgrade path (`docs/ROADMAP.md`) |
| **HRR** — Plate (1995); FHRR variants | Circular-convolution binding of role-filler pairs into compositional holograms | `vsa/ops.py` implements three algebras: `conv` (HRR proper), `perm` (permutation binding — the default, because it maps to binary HDC hardware), `bag` (superposition-only ablation) |
| **RaBitQ** — arXiv:2409.09913 | Theoretical error bounds for binary quantization with randomized rotation | The `rabitq` codec (96 B/vector): JL-style rotation before binarization restores discrimination that naive binarization loses on sparse embeddings |
| **MINJA** — arXiv:2503.03704 | Query-only memory injection: an attacker poisons memory *through the agent's own write-back* of retrieved content — no memory-bank access needed | The **contagion guard** (`security/injection.py`): quarantined source text is a tainted corpus; any re-ingest with ≥50% sentence-level token overlap (or a verbatim quote) is auto-quarantined. Catches the MINJA re-ingestion loop even when edits defeat every regex pattern |
| **InjecMEM** — arXiv:2505.17868 | Taxonomy of memory-injection attacks on RAG/agent memory | The pattern-based first-line detector: high-risk patterns quarantine, medium-risk patterns flag provenance, benign exceptions ("never ignore…") are exempted |
| **A-MEM** — arXiv:2502.12110 | Zettelkasten-style note linking and memory evolution | The Trace's edge set (`CONTRADICTS`, `TEMPORALLY_PRECEDED_BY`, `EXTRACTED_FROM`) plus entity-hop expansion gives the associative-linking effect deterministically, without LLM-generated notes |
| **Mem0** — arXiv:2504.19413 | Memory as an extracted-fact layer with a simple API surface | The `Memory` API (`add/search/get_all/history/delete`) is Mem0-compatible so migration is a one-liner; the migration importers consume Mem0/Zep/Chroma exports |

## Aligned by convergence (independent implementation, same conclusion)

| Paper | Idea | Context-M counterpart |
|---|---|---|
| **MemOS / MemCube** — arXiv:2507.03724 | Memory as a first-class OS-managed resource; unified storage unit across memory types | The Memory-Git commit DAG + lifecycle manager + federation layer are the same "OS for memory" thesis, executed deterministically. MemCube's cross-memory-type encapsulation maps to our episodic/semantic/derived fact types |
| **SleepGate** — arXiv:2603.14517; **SCM** — arXiv:2604.20943 | Sleep-inspired consolidation: replay, interference resolution, selective forgetting | The interference-aware lifecycle (`trace/lifecycle.py`) assesses each candidate fact against existing memory *before* commitment — interference is handled at write time rather than in a deferred sleep pass; `cortexm consolidate` runs the retention/consolidation sweep |
| **GHRR** — arXiv:2405.09689 | Non-commutative binding to encode role order | Evaluated; the default permutation algebra is commutative, but roles are fixed vectors so order ambiguity never arises in our fact encoding. Non-commutative binding is noted for sequence-sensitive encodings (roadmap) |
| **qFHRR** — arXiv:2604.25939 | Quantized Fourier HRR for edge efficiency | Our binary/rabitq codec tiers attack the same problem from the quantization side and are measured in `docs/COMPRESSION.md` |
| **Synapse** — arXiv:2601.02744; **NeuSymMS** — arXiv:2605.17596 | Episodic-semantic separation + neuro-symbolic fusion beats pure-neural memory | Same architecture thesis (symbolic Trace + VSA Palace + binding bridge); our ablation (`vector_only` baseline) reproduces their reported gap |
| **AMA-Bench** — arXiv:2602.22769 | Long-horizon memory fails on causality and objective information | Our event/provenance model (dated events, source hashes) addresses the objective-information axis; causal edges are roadmap |

## Deliberately rejected (with reasons)

| Technique | Why rejected |
|---|---|
| **LLM-in-loop extraction** (Mem0, Zep, A-MEM, HippoRAG) | Breaks μ=0: ingest cost scales with tokens, adds latency and API-key dependence, and makes the write path non-reproducible. Our 60-pattern deterministic extractor trades recall on exotic phrasings for a 100× cost advantage and bit-exact runs |
| **LLM judge as default** (canonical BEAM protocol) | The judge slot exists (`llm_judge=`), but the default is a deterministic nugget judge so anyone can reproduce our numbers offline for $0 |
| **Embedding models on the write path** | Even a local sentence-transformer is a model download + GPU/CPU budget. The signed feature-hashing embedder keeps μ=0 true at the embedding layer; `EmbeddingProvider` is the swap-in seam for model-based embeddings |
| **Graph databases as a hard dependency** (ArcadeDB, Neo4j) | Embedded-first deployment (single binary, SQLite) is the distribution moat. The `TraceStore` API is backend-swappable; a graph backend is a port, not a rewrite |

## Reading list (survey-level)

- *AI Meets Brain: A Unified Survey on Memory Systems* — arXiv:2512.23343
- *A Survey on the Security of Long-Term Memory in LLM Agents* — arXiv:2604.16548
- [memorypapers.org](https://memorypapers.org) — 200+ paper index for LLM/agent memory
- Mem0 blog, *State of AI Agent Memory 2026* — industry benchmark landscape (LoCoMo / LongMemEval / BEAM)

## How this survey was produced

Live web + arXiv search (August 2026) across: neuro-symbolic agent memory,
VSA/HRR binding efficiency, temporal knowledge graphs, memory-injection
defense, long-horizon benchmarks, memory consolidation, and quantized
vector search. Findings were triaged into *adopt / align / reject* within
one session; the adopted items above cite the code paths where they landed.
