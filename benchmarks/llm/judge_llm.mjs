// BEAM-style LLM judge — canonical-protocol replication.
//
// BEAM (arXiv:2510.27246) grades memory systems with an LLM judge scoring
// whether the retrieved context is SUFFICIENT to answer a probe. The original
// uses gpt-5; this harness replicates the *protocol* (context-sufficiency,
// nugget grounding, abstention checks) with a pluggable judge model whose
// identity is recorded in every result file. It exists to cross-check the
// deterministic offline judge — two independent graders must agree before a
// number goes on the leaderboard.
//
// Usage:
//   node judge_llm.mjs items.jsonl out.jsonl
// items.jsonl rows: {id, ability, question, expected, context}
// out.jsonl   rows: {id, ability, score, reason, judge_model, usage, cached}

import fs from "node:fs";
import { chatJSON, mapLimit, readJSONL } from "./common.mjs";

const RUBRIC = {
  AB: `ABSTENTION probe. The correct behaviour is to NOT answer: the memory
does not contain this information. Score 1.0 if the context does NOT contain
the requested attribute (no fabrication, no related-but-wrong fact presented
as an answer). Score 0.0 if the context contains a fabricated or irrelevant
value presented as the answer.`,
  DEFAULT: `CONTEXT-SUFFICIENCY probe. Score 1.0 if an LLM reader could answer
the question correctly using ONLY the context (all needed nuggets present).
Score 0.5 if the context contains a relevant but partial/incomplete basis.
Score 0.0 if the key nugget is missing or the context would mislead.`,
};

function judgePrompt(item) {
  const rubric = RUBRIC[item.ability] || RUBRIC.DEFAULT;
  return [
    { role: "system", content:
      "You are a strict benchmark grader for long-horizon agent memory. You " +
      "grade whether a RETRIEVED CONTEXT BLOCK is sufficient to answer a " +
      "question. You are grading the memory system, not writing the answer. " +
      "Reply with strict JSON only." },
    { role: "user", content:
`Probe (ability ${item.ability}): ${item.question}

Ground-truth nuggets the answer requires:
${(item.expected || []).map(e => `- ${e}`).join("\n")}

Retrieved context block:
"""
${item.context}
"""

Rubric — ${rubric}

Also check: dates/intervals in the context that contradict the ground truth
make the score 0.0 (misleading beats missing).

Reply with ONLY: {"score": 0.0 | 0.5 | 1.0, "reason": "<=20 words"}` },
  ];
}

async function main() {
  const [inPath, outPath] = process.argv.slice(2);
  if (!inPath || !outPath) {
    console.error("usage: node judge_llm.mjs <items.jsonl> <out.jsonl>");
    process.exit(1);
  }
  const items = readJSONL(inPath);
  // resumable: previously scored ids are kept, never re-billed
  const done = new Map();
  if (fs.existsSync(outPath)) {
    for (const r of readJSONL(outPath)) {
      if (typeof r.score === "number") done.set(r.id, r);
    }
  }
  const todo = items.filter(it => !done.has(it.id));
  console.error(`judging ${todo.length}/${items.length} items ` +
    `(${done.size} cached)...`);
  const t0 = Date.now();

  const out = await mapLimit(todo, Number(process.env.LLM_CONCURRENCY || 2),
    async (it) => {
      let row;
      try {
        const r = await chatJSON(judgePrompt(it), {
          tag: "beam-llm-judge", temperature: 0.0 });
        let score = typeof r.json?.score === "number" ? r.json.score : null;
        if (score !== null) score = Math.max(0, Math.min(1, score));
        row = {
          id: it.id, ability: it.ability, score,
          reason: (r.json?.reason || "").slice(0, 200),
          judge_model: r.model, usage: r.usage, cached: r.cached,
        };
      } catch (e) {
        row = { id: it.id, ability: it.ability, score: null,
                reason: "judge-error: " + e.message, judge_model: null };
      }
      // per-item incremental flush: a killed run keeps every finished item
      if (row.score !== null) {
        fs.appendFileSync(outPath, JSON.stringify(row) + "\n");
      }
      return row;
    });

  // summary (rows were flushed per item above)
  const all = [...done.values(), ...out.filter(r => typeof r.score === "number")];
  const scored = out.filter(o => typeof o.score === "number");
  const mean = scored.length
    ? (scored.reduce((a, o) => a + o.score, 0) / scored.length) : null;
  const tokens = out.reduce((a, o) => a + ((o.usage?.total_tokens) || 0), 0);
  console.error(`judged ${scored.length}/${out.length} in ${((Date.now()-t0)/1000).toFixed(0)}s; ` +
    `batch mean=${mean === null ? "n/a" : mean.toFixed(3)}; tokens=${tokens}; ` +
    `total scored so far=${all.length}`);
}

main().catch(e => { console.error(e); process.exit(1); });
