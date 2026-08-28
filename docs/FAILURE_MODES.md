# Failure Modes — Where the μ=0 Extractor Breaks

This document is the answer to the only question that matters about the
benchmark numbers: **what happens on text the extractor was not written
against?**

The in-distribution (ID) benchmark scores 100% because the synthetic corpus
generator and the pattern extractor were authored together — the corpus
renders facts through the same template families the patterns match. That
makes ID numbers an *upper bound for template-shaped text*, not a
capability claim. The out-of-distribution (OOD) benchmark below measures the
honest generalization gap: ground-truth facts were re-rendered by an
independent LLM (never shown the templates) in six styles, and the same
probe/judge pair was reused so ID-vs-OOD deltas are apples-to-apples.

## 1. The headline numbers

| Corpus style | Extraction recall | End-to-end (10 abilities) | With async LLM enrichment |
|---|---|---|---|
| **In-distribution (templates)** | ~1.00 (by construction) | 1.000 | — |
| OOD paraphrase | 0.094 ± 0.094 | 0.282 | — |
| OOD negation | 0.756 ± 0.033 | 0.693 | 0.657 |
| OOD indirect speech | 0.449 ± 0.102 | 0.486 | 0.493 |
| OOD informal/slang | 0.051 ± 0.059 | 0.150 | 0.171 |
| OOD non-English | 0.000 ± 0.000 | 0.157 | 0.164 |
| OOD code-switching | 0.579 ± 0.181 | 0.607 | 0.586 |

Read this table before citing any Context-M number. The real capability on
naturally-phrased English is **~9-28%**, not 100%. Non-English ingest is
**zero** without the LLM fallback.

## 2. Which facts survive re-phrasing (and which die first)

Mean extraction recall by fact type across all OOD styles (worst first):

| Fact type | Recall | Dominant failure shape |
|---|---|---|
| alias/nickname | 0.00 | "Going by Priya" — verb not in pattern library |
| name | 0.08 | elliptical self-introduction |
| skill | 0.14 | "comfortable with TypeScript, Swift, and Python" — list-compressed |
| hobby | 0.15 | "you'll find me scaling rock climbing walls" |
| city | 0.25 | "Berlin's been my home base" — possessive idiom |
| standing instruction | 0.26 | "keep things brief in our chats" |
| preference | 0.26 | "switched from X to Y" (flip, not preference statement) |
| project | 0.28 | "wrapping up Project Falcon" |
| birthday | 0.29 | "my birthday lands on April 18" |
| family | 0.33 | "My sister Nadia goes by Williams professionally" |
| manager/team/tech | 0.33-0.42 | multi-hop chains restated conversationally |
| employment | 0.46 | "took a position at Google as a researcher" |
| event | 0.46 | date formats drift from pattern anchors |
| left job / moved | 0.50-0.52 | change-of-state verbs survive best |

Pattern families with the most surface freedom (change-of-state: "left",
"moved") generalize best; identity and preference statements are the most
template-bound and collapse first.

## 3. Worked examples (real OOD renderings, real misses)

From persona `user0`, style `paraphrase`:

| Utterance (as rendered) | Ground truth | Extracted? | Why it failed |
|---|---|---|---|
| "Going by Priya, and I'm currently diving into research at Databricks." | name + employer + role | ✗ | no "my name is" anchor; "diving into research at" is not a works-at verb shape |
| "April 2024 marked when I started there." | employment start date | ✗ | "there" requires coreference to Databricks; date pattern needs an explicit org |
| "Berlin's been my home base for a while now." | lives_in Berlin | ✗ | possessive idiom; "home base" not in lives-in verb family |
| "Been thinking about my coffee habits lately - switched from oat milk lattes to straight espresso." | preference flip | ✗ | "switched from X to Y" encodes old→new, pattern expects "I prefer X over Y" |
| "Tech-wise, I'm comfortable with TypeScript, Swift, and Python." | 3 skills | ✗ | list-compressed values; skill pattern is single-value ("I know X") |
| "After wrapping up at Databricks in May 2025, I took a position at Google as a researcher." | left + joined | ✓ | "took a position at" is close enough to the joined-verb family |

## 4. Root causes, ranked by damage

1. **Template-bound verb lexicons.** The pattern library encodes ~10 verbs
   per relation ("work at", "joined", "now at"). Natural language has
   dozens more ("diving into", "finding my stride at", "wrapping up at").
   This alone explains most of the paraphrase gap.
2. **Coreference and ellipsis.** Real speakers compress: pronouns, "there",
   dropped subjects. Deterministic patterns cannot resolve "I started
   there" without discourse state.
3. **List-compressed multi-facts.** One sentence carrying 3 facts defeats
   single-value capture.
4. **Language boundary.** Zero non-English coverage — by design, since the
   pattern library is English-only. This is the cleanest argument for the
   async LLM enrichment fallback.
5. **Register mismatch.** Slang/typo register ("ya i moved to lisbon lol")
   breaks capitalization assumptions the entity regexes rely on.

## 5. What actually helps

* **Change-of-state patterns generalize** (left_job 52%, moved 50%) —
  temporal chains degrade gracefully while identity facts collapse.
* **Async LLM enrichment recovers 1-2 points on the hardest styles**
  (informal 0.157→0.171, non-English 0.157→0.164) but is **not** a rescue:
  enriched facts are confidence-capped (0.85), arrive without the
  bi-temporal chain structure the contradiction engine needs, and can
  *hurt* styles where patterns already work (negation 0.693→0.657).
  Enrichment today surfaces facts; it does not reconstruct timelines.
* **The VSA layer keeps some recall alive** even when extraction fails
  (e2e > extraction recall on every style) because chunk-level vectors
  still match lexically — partial credit, not answers.

## 6. Implications for practitioners

* For **structured, template-shaped input** (forms, HR systems, explicit
  "I work at X" statements), the μ=0 path delivers what the ID benchmark
  shows: free, fast, deterministic, auditable extraction.
* For **free-form human conversation**, budget for the hybrid policy:
  μ=0 first, async LLM enrichment for low-signal chunks, and expect
  recall in the 25-70% band depending on register — not the ID numbers.
* For **non-English corpora**, μ=0 is currently unusable; enrichment is
  mandatory.
* When citing Context-M benchmarks, cite the OOD table above. The ID table
  is a regression harness (did we break template extraction?), not a
  capability claim.

## 7. How to reproduce

```bash
python benchmarks/run_ood_pipeline.py --personas 4 --target-tokens 20000
# per-style artifacts: benchmarks/results/ood/<style>.json
# renderer provenance: benchmarks/ood/rendered_p4.jsonl (LLM-rendered,
#   fact manifests with conveyed/missed tracking per session)
```

Limitations of this methodology, stated plainly: the OOD renderings were
produced by an LLM (glm-4-plus), not by human annotators; the ground-truth
fact manifests were authored by the same party that authored the patterns
(though the *renderings* were not); renderer omissions (3/714 facts) are
excluded from extraction recall and tracked separately. A human-written
held-out set would be stricter still — the numbers above should be read as
an upper bound on OOD performance.
