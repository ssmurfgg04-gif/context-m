// OOD (out-of-distribution) conversation renderer.
//
// Purpose: break the generator<->extractor circularity that inflated the
// in-distribution benchmark. Personas' GROUND-TRUTH fact registries are
// exported from Python as a manifest; this tool re-renders each session's
// facts as natural chat messages in styles the regex extractor was NEVER
// tuned against (paraphrase / negation / indirect / informal / non_english /
// code_switch). Probes and judges stay identical, so the ID->OOD score drop
// is a pure generalization-gap measurement.
//
// Usage:
//   node render_ood.mjs manifest.json out.jsonl --style paraphrase
//   node render_ood.mjs manifest.json out.jsonl --style all

import path from "node:path";
import { chatJSON, mapLimit, readJSONL, writeJSONL } from "./common.mjs";

const STYLES = {
  paraphrase: `Reword EVERYTHING with constructions a template author would not reach for.
FORBIDDEN phrasings: "My name is X", "I work at X", "I live in X", "I prefer X",
"I left X", "I joined X", "My birthday is", "My sister X works at", "I know X",
"I'm working on X". Use varied natural alternatives instead.`,
  negation: `Express states and changes through negation and contrast where natural:
"I'm not with Google anymore", "I don't touch coffee these days", "It's been a
while since I lived in Toronto". Positive statements are still allowed when
negation would be absurd, but prefer negated/contrastive forms.`,
  indirect: `Convey facts obliquely — as asides, rhetorical questions, reported speech,
or third-person self-reference: "Did I mention I finally left Stripe?",
"My sister's over at Netflix now, loving it", "This Toronto girl relocated,
by the way". Never state a fact in plain subject-verb-object form.`,
  informal: `Heavy internet-casual register: lowercase, slang, abbreviations, minimal
punctuation, occasional typos: "ya i moved to lisbon lol", "quit google btw",
"sis works at stripe now i think". Keep it readable and factual.`,
  non_english: `Convey the facts in a NON-ENGLISH language (Spanish, French, German, or
Mandarin — your choice per message, mixing is fine). Keep proper nouns exactly
as given (Alice Johnson, Google, Toronto, Project Falcon...). The facts must
still be recoverable to a bilingual reader.`,
  code_switch: `Code-switch: mix English with another language WITHIN sentences
(Spanglish, Franglais, Denglish...). Keep proper nouns exactly as given.
Example register: "So yo dejé Google en marzo, and now estoy en Anthropic
haciendo ML stuff."`,
};

const STYLE_KEYS = Object.keys(STYLES);

function renderPrompt(persona, styleKey) {
  const styleGuide = STYLES[styleKey];
  const sessions = persona.sessions.map(s =>
    `SESSION ${s.session} (date ${s.date}):\n` +
    s.facts.map(f => `  - [${f.id}] ${f.text}`).join("\n")
  ).join("\n\n");

  return [
    { role: "system", content:
      "You are simulating a real user chatting with an AI assistant across " +
      "multiple sessions. You will be given ground-truth facts grouped by " +
      "session. Write the chat messages that convey EXACTLY those facts — " +
      "no fact may be dropped, no new factual claims about the user may be " +
      "invented (small talk and filler are fine). Output strict JSON only." },
    { role: "user", content:
`Persona: ${persona.full_name} (chat user_id "${persona.user_id}").
Style requirement — ${styleKey}:
${styleGuide}

Facts to convey, grouped by session (session dates matter; mention dates/years
naturally when the fact includes them):
${sessions}

Rules:
1. 4 to 8 short messages per session, as the chat user.
2. Every fact id must be conveyed somewhere in its session (or an adjacent
   one if it flows better, but keep it in the same session when possible).
3. Keep ALL proper nouns (names, employers, cities, projects, teams,
   technologies, values like "oat milk lattes") EXACTLY as written.
4. Do NOT use bullet lists or metadata; write real chat messages.
5. Small talk between facts is encouraged for realism.

Reply with ONLY this JSON shape:
{"sessions":[{"session":0,"messages":["...","..."],"conveyed":["f1","f2"]},
 ...]}
"conveyed" lists every fact id you actually conveyed.` },
  ];
}

async function renderPersona(persona, styleKey) {
  const r = await chatJSON(renderPrompt(persona, styleKey), {
    tag: `ood-render:${styleKey}`, temperature: 0.8 });
  const j = r.json;
  if (!j || !Array.isArray(j.sessions)) {
    return { user_id: persona.user_id, style: styleKey, sessions: [],
             model: r.model, error: "bad-json" };
  }
  const declared = new Set(persona.sessions.flatMap(s => s.facts.map(f => f.id)));
  const conveyed = new Set(j.sessions.flatMap(s => (s.conveyed || [])
    .filter(id => declared.has(id))));
  return {
    user_id: persona.user_id,
    style: styleKey,
    sessions: j.sessions
      .filter(s => Array.isArray(s.messages))
      .map(s => ({ session: s.session ?? 0, messages: s.messages })),
    conveyed: [...conveyed],
    missed: [...declared].filter(id => !conveyed.has(id)),
    model: r.model,
  };
}

async function main() {
  const [inPath, outPath, ...rest] = process.argv.slice(2);
  if (!inPath || !outPath) {
    console.error("usage: node render_ood.mjs <manifest.json> <out.jsonl> [--style k|all]");
    process.exit(1);
  }
  const styleFlag = rest.find(a => a.startsWith("--style"));
  const styleArg = styleFlag ? styleFlag.split("=")[1] || rest[rest.indexOf(styleFlag) + 1] : "all";
  const styles = styleArg === "all" || !styleArg ? STYLE_KEYS : [styleArg];

  const manifest = JSON.parse((await import("node:fs")).readFileSync(inPath, "utf8"));
  const jobs = [];
  for (const p of manifest.personas) {
    for (const s of styles) jobs.push({ p, s });
  }
  console.error(`rendering ${jobs.length} persona-style combos...`);

  const results = await mapLimit(jobs, Number(process.env.LLM_CONCURRENCY || 2),
    async ({ p, s }) => {
      try {
        const out = await renderPersona(p, s);
        console.error(`  ok ${p.user_id}/${s}: ${out.conveyed?.length ?? 0} conveyed, ${out.missed?.length ?? 0} missed`);
        return out;
      } catch (e) {
        console.error(`  FAIL ${p.user_id}/${s}: ${e.message}`);
        return { user_id: p.user_id, style: s, sessions: [], conveyed: [],
                 missed: [], error: e.message };
      }
    });

  writeJSONL(outPath, results);
  const totalMissed = results.reduce((a, r) => a + (r.missed?.length || 0), 0);
  const totalConveyed = results.reduce((a, r) => a + (r.conveyed?.length || 0), 0);
  console.error(`done: ${totalConveyed} conveyed, ${totalMissed} missed by renderer ` +
    `(renderer omissions are tracked separately from extraction failures)`);
}

main().catch(e => { console.error(e); process.exit(1); });
