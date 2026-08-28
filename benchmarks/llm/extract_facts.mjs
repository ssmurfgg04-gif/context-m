// LLM reference fact extractor.
//
// Two uses:
//   1. REFERENCE EXTRACTION on real-world corpora — the yardstick the μ=0
//      pattern extractor is compared against (recall/precision/cost/time).
//   2. ASYNC ENRICHMENT FALLBACK — when pattern confidence is low, the same
//      extraction contract is used post-store, off the μ=0 critical path.
//
// Usage:
//   node extract_facts.mjs items.jsonl out.jsonl [--enrich]
// items.jsonl rows: {id, text, subject?}
// out.jsonl rows:    {id, facts: [{subject, relation, value, confidence}],
//                     model, usage, cached}

import { chatJSON, mapLimit, readJSONL, writeJSONL } from "./common.mjs";

const ENRICH_MODE = process.argv.includes("--enrich");

function prompt(item) {
  return [
    { role: "system", content:
      "You extract factual memory triples from chat/message text. Extract " +
      "only facts STATED in the text (no inference beyond pronoun binding). " +
      "Reply with strict JSON only." },
    { role: "user", content:
`Text${item.subject ? ` (speaker: ${item.subject})` : ""}:
"""
${item.text}
"""

Extract facts as triples {subject, relation, value}:
- subject: who the fact is about (the speaker's name if known, else "${item.subject || "user"}")
- relation: snake_case verb phrase (works_at, lives_in, prefers, uses,
  left, joined, moved_to, knows, manages, on_team, shipped, owns,
  allergic_to, studied, speaks, ...)
- value: the object, kept verbatim and short (<= 8 words)
- confidence: 0.0-1.0 (1.0 = explicitly stated, 0.5 = hedged/indirect)

Rules:
- Dates/quantities stay inside the value or relation (e.g. relation
  "joined", value "Anthropic in March 2026").
- Negated facts: use relation "left" / "does_not" style, value = the thing
  negated.
- Ignore opinions about third parties' work quality, questions, greetings.
${ENRICH_MODE ? "- This is an enrichment pass over text a regex extractor already missed; be thorough on indirect phrasing." : ""}

Reply with ONLY: {"facts":[{"subject":"...","relation":"...","value":"...","confidence":1.0}]}` },
  ];
}

async function main() {
  const [inPath, outPath] = process.argv.slice(2).filter(a => !a.startsWith("--"));
  if (!inPath || !outPath) {
    console.error("usage: node extract_facts.mjs <items.jsonl> <out.jsonl> [--enrich]");
    process.exit(1);
  }
  const items = readJSONL(inPath);
  console.error(`extracting from ${items.length} items (enrich=${ENRICH_MODE})...`);
  const t0 = Date.now();

  const out = await mapLimit(items, Number(process.env.LLM_CONCURRENCY || 2),
    async (it) => {
      try {
        const r = await chatJSON(prompt(it), {
          tag: ENRICH_MODE ? "llm-enrich" : "llm-reference-extract",
          temperature: 0.0 });
        const facts = Array.isArray(r.json?.facts) ? r.json.facts : [];
        return { id: it.id, facts, model: r.model, usage: r.usage,
                 cached: r.cached };
      } catch (e) {
        return { id: it.id, facts: [], error: e.message };
      }
    });

  writeJSONL(outPath, out);
  const nFacts = out.reduce((a, o) => a + o.facts.length, 0);
  const tokens = out.reduce((a, o) => a + ((o.usage?.total_tokens) || 0), 0);
  const fresh = out.filter(o => !o.cached).length;
  console.error(`extracted ${nFacts} facts from ${items.length} items in ` +
    `${((Date.now()-t0)/1000).toFixed(0)}s (${fresh} fresh calls, ${tokens} judge tokens)`);
}

main().catch(e => { console.error(e); process.exit(1); });
