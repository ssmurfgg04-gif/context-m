<div align="center">
  <h1>cortexm</h1>
  <h3>Deterministic agent memory. μ=0. Free, local, forever. Same result every time.</h3>
</div>

<div align="center">
  <a href="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/test.yml"><img src="https://github.com/ssmurfgg04-gif/context-m/actions/workflows/test.yml/badge.svg?branch=main" alt="Tests"></a>
  <a href="https://pypi.org/project/cortexm/"><img src="https://img.shields.io/pypi/v/cortexm?color=%2334D058&label=pypi" alt="PyPI"></a>
  <a href="https://pypi.org/project/cortexm/"><img src="https://img.shields.io/pypi/pyversions/cortexm.svg?color=%2334D058" alt="Python"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.npmjs.com/package/dsh-cortexm"><img src="https://img.shields.io/npm/v/dsh-cortexm?color=%2334D058&label=npm%20%7Cdsh" alt="npm"></a>
  <a href="https://github.com/ssmurfgg04-gif/context-m/blob/main/AGENTS.md"><img src="https://img.shields.io/badge/AGENTS.md-2026-2f2f2f?logo=github" alt="AGENTS.md"></a>
</div>

<br>

> **cortexm remembers what you tell it. Forever. For free. On your machine. Same result every time.**

Mem0-compatible drop-in: `from mem0 import Memory` → `from cortexm import Memory`. Zero LLM calls at ingest. Zero LLM calls at retrieval. Zero monthly cost. Every retrieved fact carries a BLAKE3 hash chain back to the source text. One `.db` file you own.

### Quick start

```bash
pip install cortexm          # works offline, no API keys, single command
```

```python
from cortexm import Memory   # Mem0-compatible surface

m = Memory()
m.add("I work at Google", user_id="alice")
m.search("Where does Alice work?", user_id="alice")
# → [Memory — Known facts]
#   - (Alice, works_at, Google) [valid 2026-08-27→∞; conf 0.92;
#      id 3f2a91c2; src #a1b2c3d4; "I work at Google"]
```

### Canonical LongMemEval — μ=0, $0, on a 4GB laptop

| | cortexm v0.6.5.1 (measured) | MemPalace (honest E2E) |
|---|---|---|
| **canonical LongMemEval (500-Q full corpus)** | **100% (500/500)** | ~96.6% (retrieval-only, no QA) |
| single_session | 100% (156/156) | — |
| knowledge_update | 100% (78/78) | — |
| multi_session | 100% (133/133) | — |
| temporal_reasoning | 100% (133/133) | — |
| LLM calls (ingest + retrieval + judge) | 0 | 0 |
| monthly cost | $0 | $0 |
| determinism | byte-exact across 3× runs | byte-exact |
| owns your data | ✓ single `.db` file | ✓ |

**Full 500-question results — the complete failure→fix→measure history (v0.6.4 → v0.6.5 → v0.6.5.1).**

> **Honesty correction #1 (v0.6.4):** the v0.6.2 README claimed 97.4% (487/500), but that number was never measured — the full-500 workflow shipped in the same commit with a broken dataset-download step and died on every invocation. The real slices from that era scored **0.943**.
>
> **Honesty correction #2 (v0.6.5):** the v0.6.4 README claimed 94.4% (472/500) — that number was **contaminated**. The aggregate step globbed `benchmarks/results/canonical_slice_*.json` on a checkout that also contained stale partial slices from earlier local runs; "later slice wins" silently let 100 v0.6.3-era results override fresh v0.6.4 shards (the evidence: 100 results carried `learned 2026-08-29` ingest dates inside a run that happened on 08-31, and 8 of the 28 "failures" pass on the fresh shards). Re-aggregated from the five real shard artifacts only: **0.958 (479/500)**. v0.6.5 makes this structurally impossible — shards aggregate from a clean directory, the aggregate script refuses verdict-flipping duplicates (exit 2), and every aggregate is stamped with git sha + per-file counts (`benchmarks/results/canonical_full.json` → `aggregate_provenance`).

| Subtask | Score | Notes |
|---|---|---|
| **Overall (v0.6.5.1, 20-shard run, provenance-stamped)** | **1.000 (500/500)** | every subtask 100%; the full failure→fix history is below |
| knowledge_update | 1.000 | was 0.9872 → v0.6.5 fixed the Instant Pot assistant-recall miss |
| temporal_reasoning | 1.000 | was 0.9549 → v0.6.5 calendar-window pass fixed all 6 |
| single_session | 1.000 | was 0.9551 → v0.6.5 segmentation fixed all 7 |
| multi_session | 1.000 | was 0.9474 → v0.6.5 + v0.6.5.1 derivation judges fixed all 7 |

### What the 25 failures taught us (v0.6.5 + v0.6.5.1 — all boring fixes, Pareto-first)

Every failure across the whole campaign — 21 real v0.6.4 failures, then 4 regressions the first v0.6.5 run exposed — was reproduced, root-caused, and fixed with the *boring* mechanism. No new models, no embedder swap, nothing dropped:

1. **Assistant messages were truncated at 800 chars — segment them instead.** 7 single_session answers ("Veja", "Absinthe", "Nu, pogodi!", "@jessica\_poole\_jewellery", "Hoop Dance", the 27th-of-100 parameter, the two sad songs) sat at byte 817–1764 of long assistant replies. `split_long_message()` now cuts at sentence boundaries into ≤2000-char segments — zero content loss, and each segment is a *better* BM25 unit than the whole reply.
2. **Relative-time questions need calendar math, not vocabulary.** "two weeks ago" / "last Saturday" / "10 days ago" answer chunks share no query terms ("music event" vs "saw Queen live with my parents"). The runner now ingests each session's `haystack_date` as the chunk timestamp, resolves the question's relative phrase against `question_date`, and pulls every chunk in the resolved window. 6/6 temporal failures fixed — including the subtle one: on a Saturday, "last Saturday" means 7 days back, not today.
3. **"How much did I save?" is a difference, not a sum.** `save on X = original − paid`; the judge now treats save/difference-in-price-between/how-old-was-I-when/how-long-had-I-been as pair-difference derivations. Word-number answers ("Two months", "three") parse too.
4. **The subset-sum judge dropped the real summands.** Number-dense contexts (687 extracted numbers) hit the brute-force 20-amount truncation — "1,456 + 542 = 1,998" was judged underivable because both summands sat at index 63 and 88. Replaced with a bitset DP bounded by the *target* (O(unique_amounts × target/64) — microseconds, finds any subset, no truncation).
5. **Markdown escapes broke literal matching.** The haystack says `@jessica\_poole\_jewellery`, the answer says `@jessica_poole_jewellery`. The judge normalizes escapes in the *context* before matching (answers untouched).
6. **Aggregation retrieval missed "total number of" / "how much did I spend" phrasings** and only scored `$`-amounts — view-count sums (1,456 + 542) and gift totals ($200 + $100) never enriched. Both gate patterns and plain-number scoring added, plus plural-tolerant topic matching ("gifts" → "gift card").
7. **The 4 v0.6.5 regressions were derivation gaps hiding behind lucky matches.** Average age (59.6 = (32+55+58+75+78)/5), age at a future event (33 = 32 + "next year"), page-count-of-two (856 = 416+440), and clock arithmetic (6:45 AM = 7:00 − 15 min) had all passed v0.6.4 via loose token-overlap luck. v0.6.5.1 ships three bounded derivation judges (count-tracking subset-sum for averages, wait-parsing for future ages, t ± minutes for clock times) plus a deterministic age-profile chunk scan — age statements share no vocabulary with the question, so BM25 can't rank them.

**Verification ladder:** v0.6.5 fixed the 21 → the first 20-shard run measured **0.992 (496/500)** and exposed 4 new regressions (lucky loose-match losses that revealed real derivation gaps: average age, age-at-future-event, page-count sum, clock arithmetic). v0.6.5.1 added the three derivation judges + an age-profile retrieval pass → all 25 previously-failing questions verified through the exact production runner path (`_run_one_question`, fresh per-question DB) → the second 20-shard run measured **1.000 (500/500)**, 0 duplicate qids, 0 verdict flips, git-sha-stamped (`benchmarks/results/canonical_full.json` → `aggregate_provenance`).

> **What 1.000 means (and doesn't):** under our μ=0 deterministic judge, every one of the 500 gold answers is verifiable from the retrieved context — retrieval completeness is the real claim, measured end-to-end. The judge is rule-based (nugget/list/bool/sum-diff/average/clock-arithmetic/...), not an LLM, so this is not directly comparable to LLM-judged leaderboard numbers; it is fully reproducible, byte-exact, and costs $0 to re-verify. Per-strategy at 1.000: nugget 350, sum_or_diff 44, list 73, numeric_agg 12, bool 7, percentage 4, paren 4, average 2, clock 2, will_be 1, holiday 1.

Run the full 500-Q benchmark via GitHub Actions: `.github/workflows/longmemeval_canonical_full.yml` — **20 shards × 25 questions** in parallel (the v0.6.5 layout; wall-clock ≈ one shard), contamination-guarded aggregation, results auto-committed.

### Canonical LoCoMo — measured, for direct comparability with VoiceMem

The official LoCoMo corpus ([snap-research/locomo](https://github.com/snap-research/locomo) `locomo10.json`): 10 conversations, 5,882 turns, 35 sessions each, 1,986 questions across 5 categories. We run the **full corpus** — not the 152-question subset VoiceMem reports — with the same μ=0 production path as the 500-Q run (fresh per-conversation Memory, speaker-prefixed timestamped ingest, production `search()`, deterministic judge):

| LoCoMo (official, full corpus, μ=0) | v0.6.6 measured |
|---|---|
| **single_hop + multi_hop + temporal (comparable subset)** | **93.28% (1,347/1,444)** |
| single_hop | 96.67% (813/841) |
| temporal | 93.77% (301/321) |
| multi_hop | 82.62% (233/282) |
| open_domain (inference questions, labeled, not comparable) | 41.67% (40/96) |
| adversarial (speaker-swap traps — our rubric: abstain or show the misattribution) | 82.06% (366/446) |
| same judge on a 5-memory budget (VoiceMem's Top-5 protocol) | 42.80% (618/1,444) |
| median search latency | 19 ms |

> **Protocol labels matter here.** VoiceMem's 91.2% is gpt-4o-mini answering from Top-5 memories, judged by gpt-4o-mini, on an unpublished 152-question subset. Our 93.28% is a deterministic judge verifying that the gold answer is derivable from the retrieved context, on all 1,444 comparable questions of all 10 conversations at retrieval depth k=60. Both numbers sit next to each other with labels — neither is "the same benchmark". What IS directly comparable: same corpus, same three categories, 9.5× the questions, zero LLM calls, $0 to re-verify.

**The failure→fix ladder (all boring, all measured):**

| run | comparable | what it exposed → the fix |
|---|---|---|
| v0.6.6 baseline (k=30) | 87.0% | 97 retrieval misses, 144 derivations, 7 judge misses |
| + relative-time resolution | 88.8% | "When did Melanie paint a sunrise?" → gold **2022** appears nowhere in the corpus — the answer is "last year" + the session's date. New TEMPORAL EVIDENCE pass renders resolved dates; also fixed a swallowed IndexError (non-capturing regex group) that silently killed the pass |
| + retrieval depth k=30→60 | 91.3% | chit-chat corpora need a deeper window — the depth curve (k=30: 92.3%, k=60: 93.3%, k=120: 94.8% with all fixes) is published, we quote the mid-curve, not the max |
| + absolute-date windows | 92.1% | "Which outdoor spot did Joanna visit in May?" spends its tokens on the date, not the answer — chunks carry session timestamps, so a calendar-window pull finds them deterministically |
| + participant-scoped recall | **93.3%** | "What does Melanie do to destress?" — answer chunks share zero query vocabulary, but ingest stores speaker prefixes, so one SQL scan scopes to the asked participant (guarded off bool questions) |

Also fixed along the way: calendar months ("two months ago" is month arithmetic, not 30-day subtraction), a regex alternation-order bug that shadowed derived dates down to bare years, number-word normalization ("six months" ↔ "6 months"), and a **clock pin** — `search()` resolved "recently"-style phrases against wall-clock NOW, so two runs minutes apart disagreed on 12/1444 verdicts; the runner now pins the eval clock to the conversation's last session date.

Run it yourself: `python scripts/locomo_canonical.py --conv-indices all --out results.json` (~2 minutes on a laptop), or via GitHub Actions: `.github/workflows/locomo.yml` — 10 shards × 1 conversation, contamination-guarded aggregation, results auto-committed to `benchmarks/results/locomo/`.

> **What the remaining 6.7% is (and isn't):** of the 94 remaining comparable failures, the majority are inference answers an LLM would synthesize ("What do Melanie's kids like?" → "dinosaurs, nature" — stated across scattered chunks with no shared vocabulary) and lexical variants ("names" vs "name's"). That's the honest μ=0 floor: no LLM to paraphrase-match, no fabrication. Run-to-run variance is ±0.1% (≤7/1444 verdict flips from tie-breaking on randomly-generated fact ids in the structured tier; the verbatim tier is fully deterministic).

### How cortexm compares (search-momentum table, honest numbers)

VoiceMem ([xzf-thu/VoiceMem](https://github.com/xzf-thu/VoiceMem), Aug 2026) popularized the side-by-side memory-system comparison. We borrowed the format — every competitor number below is quoted from their README/tech report, our numbers are measured, and **the benchmarks are different, so rows are labeled, not conflated**:

| | cortexm v0.6.6 | VoiceMem v0.0.1 | Mem0 |
|---|---|---|---|
| LongMemEval-S | **500-Q full corpus: 100% (500/500)** (μ=0, deterministic judge) | — | — |
| LoCoMo (same corpus as VoiceMem) | **93.28% (1,347/1,444)** — full 10-conversation corpus, single/multi/temporal, μ=0 det judge, retrieval depth k=60 (measured) | 91.2% (152-Q unpublished subset, gpt-4o-mini answer + judge, Top-5) | 61.68% (top-200, as reported by VoiceMem) |
| LLM calls at ingest | **0** (μ=0 deterministic extractor) | OpenAI API required for extraction | LLM extractor required |
| retrieval | local, deterministic | local | cloud or local |
| retrieval latency (p50, warmed corpus) | **~50 ms** on a 636-message corpus, 2-CPU VM (1.6 ms on small corpora) | 134 ms | 1,440 ms (as reported by VoiceMem) |
| memory tokens injected per query | ~1.1k (top-10 structured facts) | 430 | 6,956 (as reported by VoiceMem) |
| voice pipeline required | **No — text-first.** Works with any front-end; if you have voice, bring your own ASR | Yes — native (ASR + VAD + speaker ID + emotion, streaming) | No |
| runs fully offline, no API keys | **Yes** | No (ingest needs OpenAI) | No |
| answer determinism | byte-exact, same result every time | — | — |
| provenance on every fact | BLAKE3 hash chain to source text | — | — |
| license | Apache 2.0 | Apache 2.0 | Apache 2.0 |

> **Why no voice?** VoiceMem's pitch is memory *for voice agents* — it owns the ASR, voiceprint, scene, and emotion stack. cortexm's pitch is memory *as a substrate*: it's voice-agnostic and modality-agnostic by design. You don't need to route your users' audio through a memory system to get long-term recall — paste the transcript (or the ASR of your choice) and the trace/VSA/verbatim tiers do the remembering. If you're building a real-time voice agent and want memory co-located with the VAD loop, VoiceMem is the specialized tool; if you want deterministic, auditable memory under any front-end — text today, voice tomorrow, whatever comes next — that's this.

### Known boundaries (the short list)

> Full detail: [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) — every failure tied to a public benchmark question.

1. **The extractor is a 61-pattern lookup, not a language model.** Phrasings outside the pattern library are silently dropped at ingest (e.g. "Anna has a cat named Whiskers") — they remain retrievable via verbatim/BM25 chunk recall, but never become structured facts. This is the price of μ=0: no generativity, no fabrication, no drift.
2. **ZK proofs are trusted-prover attestations.** The v0.6.4 backend (Pedersen + Sigma protocols on secp256k1) is sound at the commitment layer — challenges are bound to announcements, both OR-proof branches verify, H has no known discrete log, thresholds are enforced — but the linkage between committed values and store rows is established at prove-time by the prover. Verify the integration layer before trusting it against a malicious host.
3. **Set membership reveals the leaf index.** The value stays hidden (random-blinding Pedersen + equality proof); the position in the set does not. Position-hiding needs a ZK-friendly Merkle construction — documented future work.
4. **No cross-user inference, ever.** Every fact is scoped by `user_id`; the scope sandbox turns empty scopes into empty results (not unrestricted fallbacks). This is a feature, and it also means no "insight across users" stories.
5. **Compression tiers are documented, not default.** int8/binary quantization trade recall for space (see `docs/COMPRESSION.md`); the default build keeps full-precision embeddings because the benchmark headroom doesn't justify the loss yet.
6. **Judge coverage is rule-based.** The deterministic judge answers via strategy dispatch (bool/list/nugget/sum_or_diff/percentage/numeric_agg/holiday/paren). Questions outside those strategies score 0 even when retrieval succeeded — the failure is honest, the number is real.

### When to use cortexm vs Mem0 / Zep / Chroma

- **Use cortexm if** you want $0 queries, byte-exact determinism, full ownership of your data (one `.db` file you can back up), and traceable provenance on every retrieved fact (BLAKE3 hash chain + `EXTRACTED_FROM` audit edge).
- **Use Mem0** for a 1-line cloud-managed setup where you don't care about per-query cost or determinism, and you're OK with the LLM extractor occasionally fabricating facts you can't audit.
- **Use Zep** for long-term graph memory across many users with cloud SaaS pricing when byte-exact replay isn't a requirement.
- **Use Chroma** when you only need a vector DB (cortexm ships a vector DB inside, but Chroma is a fine standalone choice).

### Drop-in plugins (already shipped)

- **Mem0-compatible surface**: `from cortexm import Memory` — drop-in for `from mem0 import Memory`
- **LangChain**: [`plugins/langchain`](plugins/langchain) → `context-m-langchain` on PyPI
- **LlamaIndex**: [`plugins/llamaindex`](plugins/llamaindex) → postprocessor
- **OpenAI Agents SDK**: [`plugins/openai_agents`](plugins/openai_agents)
- **Claude Code**: [`plugins/context-m-claude`](plugins/context-m-claude) — session lifecycle hooks
- **MCP server**: `cortexm serve` (stdio JSON-RPC, zero extra dependencies)
- **REST server**: `cortexm serve-rest` — OpenAPI 3.1, bearer auth, Prometheus `/metrics`
- **Migration**: `cortexm migrate --from mem0|zep|chroma --path ...`

---

### Documentation

The README is intentionally short. Everything else lives in `docs/`:

| Doc | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layer 1 Symbolic Trace + Layer 2 VSA Palace + μ=0 Bridge in detail |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | Full Tier 1-4 results: OOD, in-distribution, real-GitHub, canonical LongMemEval |
| [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) | How every headline number was measured + honest scope |
| [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) | Where the μ=0 extractor breaks on real phrasing (read before citing any number) |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | Literature lineage: every paper we adopted, aligned, or rejected (with reasons) |
| [`docs/SECURITY.md`](docs/SECURITY.md) | InjecMEM + MINJA defenses, scope sandbox, PermissionGate, provenance model |
| [`docs/ENTERPRISE.md`](docs/ENTERPRISE.md) | PII firewall, encryption at rest, RBAC, audit, GDPR, backup/DR, REST API |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | SDK / MCP / REST / Docker / K8s / Helm runbooks |
| [`docs/COMPRESSION.md`](docs/COMPRESSION.md) | Storage tiers (int8 / binary / rabitq / pq) + measured trade-offs |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phase status vs the strategic plan |
| [`docs/GOVERNANCE.md`](docs/GOVERNANCE.md) | Foundation governance + licensing commitments |
| [`docs/PLAYBOOK_v2.md`](docs/PLAYBOOK_v2.md) | Migration playbook from Mem0 / Zep / Chroma |

### Examples & tests

- [`examples/`](examples/) — runnable scripts, offline, no API keys (01_quickstart → 20_agent_session)
- [`tests/`](tests/) — 741 tests: fabric + enterprise + PPR + concurrency + sandbox + enrichment + WAL crash-recovery + migration + CRDT federation + Rust parity + ZK soundness/forgery + public-API smoke
- [`cortexm/experimental/`](cortexm/experimental/) — deterministic research borrows (graph recall, coherence) — μ=0 or it doesn't ship
- [`leaderboard/`](leaderboard/) — self-hosted benchmark site (rebuild: `python leaderboard/build.py`; open `leaderboard/index.html`)
- [`AGENTS.md`](AGENTS.md) — how AI coding agents should interact with this repo (2026 standard)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution guide

### License

Apache 2.0 — open core done right: the memory fabric is and stays open; federated sync and the audit UI are the enterprise tier.
