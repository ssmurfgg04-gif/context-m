// QA-pair generation over real-world threads (real-GitHub corpus track).
//
// Generates questions whose gold answers are grounded in the thread text
// with a supporting quote, so retrieval can be graded by the LLM judge.
//
// Usage:
//   node qa_generate.mjs threads.jsonl out.jsonl
// threads.jsonl rows: {id, repo, title, comments: [{author, created_at, body}]}
// out.jsonl rows:     {id, questions: [{question, answer, quote}]}

import { chatJSON, mapLimit, readJSONL, writeJSONL } from "./common.mjs";

function prompt(thread) {
  const convo = thread.comments.slice(0, 24).map(c =>
    `[${c.created_at}] ${c.author}: ${c.body}`).join("\n").slice(0, 12000);
  return [
    { role: "system", content:
      "You write benchmark QA pairs grounded STRICTLY in a given thread. " +
      "Reply with strict JSON only." },
    { role: "user", content:
`Thread: ${thread.title} (repo: ${thread.repo})
"""
${convo}
"""

Write 3 to 5 benchmark questions testing LONG-HORIZEN MEMORY of this thread:
- each answerable from the thread text ALONE
- prefer: participant attributes (who uses which tool/version/OS), temporal
  facts (what happened first, when was something reported), status changes
  (was it resolved, who closed it), cross-comment references
- include at least one question whose answer requires joining two comments
- include one UNANSWERABLE question about something plausible but absent
  (answer: "NOT_IN_THREAD")

Reply with ONLY:
{"questions":[{"question":"...","answer":"...","quote":"<=25 word supporting quote"}]}` },
  ];
}

async function main() {
  const [inPath, outPath] = process.argv.slice(2);
  if (!inPath || !outPath) {
    console.error("usage: node qa_generate.mjs <threads.jsonl> <out.jsonl>");
    process.exit(1);
  }
  const threads = readJSONL(inPath);
  console.error(`generating QA for ${threads.length} threads...`);

  const out = await mapLimit(threads, Number(process.env.LLM_CONCURRENCY || 2),
    async (t) => {
      try {
        const r = await chatJSON(prompt(t), { tag: "qa-generate", temperature: 0.4 });
        const questions = Array.isArray(r.json?.questions) ? r.json.questions : [];
        return { id: t.id, questions, model: r.model };
      } catch (e) {
        return { id: t.id, questions: [], error: e.message };
      }
    });

  writeJSONL(outPath, out);
  const nQ = out.reduce((a, t) => a + t.questions.length, 0);
  console.error(`generated ${nQ} questions across ${out.length} threads`);
}

main().catch(e => { console.error(e); process.exit(1); });
