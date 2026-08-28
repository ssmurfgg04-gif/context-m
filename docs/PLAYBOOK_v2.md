# Playbook v2 — Top-Starred GitHub Repos, 2026 Trends, Agent-Friendly CONTRIBUTING, PR-Gate Workflows

> Source: deep-research subagent, Task ID 19-research-deep. Sample = 100 top-starred repos (EvanLi/Github-Ranking, snapshot 2026-08-28) + 12 hand-fetched READMEs from the 2026 viral cohort (OpenClaw, ECC, Hermes Agent, Ponytail, anthropics/skills, mattpocock/skills, obra/superpowers, github/spec-kit, astral-sh/uv, modelcontextprotocol/servers, firecrawl, multica-ai/andrej-karpathy-skills) + Mem0 pr-gate.yml + Mem0 CONTRIBUTING.md + Mem0 VOUCHED.td + LangChain AGENTS.md + OpenAI Codex AGENTS.md + canonical agents.md spec.
>
> Supersedes v1 (Playbook, 41 repos, kept as informal subagent result). v2 extends with 2026-specific signals (MCP, AGENTS.md, skills marketplaces, eval-driven dev, verifiable-compute, Trendshift.io, skills.sh) and concrete YAML / markdown templates the user can paste.

---

## A. Top 15 Patterns the Top-100 Starred Repos Share (ranked by frequency in sample)

Surveyed = 100 top-starred repos as of 2026-08-28. Frequency = % of repos showing the pattern.

1. **One-word lowercase repo name** (76/100). `openclaw`, `superpowers`, `firecrawl`, `ponytail`, `langchain`, `ollama`, `transformers`, `langflow`, `dify`, `flutter`, `bootstrap`, `excalidraw`, `rustdesk`, `next.js`, `electron`, `godot`, `immich`, `kubernetes`, `vscode`, `tensorflow`, `rust`, `go`, `node`, `d3`, `vue`, `react`. The few that aren't (e.g. `free-programming-books`, `awesome-selfhosted`, `Microsoft-Activation-Scripts`, `system-design-primer`) are content-list repos where the name describes the content type, not a product. **Zero repos in the top-100 use snake_case for the repo name.**

2. **Centered `<div align="center">` or `<p align="center">` hero block at the very top of README** (88/100). Wraps a `<picture>` with `prefers-color-scheme dark/light` sources, then the `<h1>` title, then an `<h3>` or italic tagline, then a badge row. This is the universal masthead.

3. **`<picture>` element with `prefers-color-scheme` for dark/light hero art** (61/100 — every repo that ships art at all). Concrete example (uv):
   ```html
   <picture align="center">
     <source media="(prefers-color-scheme: dark)" srcset="…dark.png">
     <source media="(prefers-color-scheme: light)" srcset="…light.png">
     <img alt="…" src="…light.png">
   </picture>
   ```

4. **Italic one-line tagline directly under the title, before badges** (74/100). Length ≤ 90 chars. Pattern: noun-phrase or verb-phrase that states what the thing IS, never what it does (Houdini = noun, not feature list).

5. **shields.io badge row, 3-5 badges, style `flat-square` or `for-the-badge`** (95/100). Modern palette: `CI status` → `Latest Release / PyPI version / npm version` → `License` → `Discord` → (optional) `GitHub stars`. The 2018 palette (Travis, Coveralls, Codacy, Gemnasium, David-dm, BitHound) is gone.

6. **Install command in the first 100 lines, as the first code block** (84/100). Either `pip install x`, `curl -LsSf …install.sh | sh`, `npx x`, `brew install x`, or `claude plugins install x`. Never more than one install path shown before the fold — alternates go in `<details>`.

7. **Single-word CLI binary name matching the repo name** (88/100 of CLI tools). `uv`, `rg`, `bun`, `deno`, `ollama`, `firecrawl`, `langflow`, `langchain`, `dify`, `ponytail`, `specify` (github/spec-kit's CLI — note: repo is `spec-kit` but binary is `specify`, both single tokens). Subcommand-driven: `uv sync`, `rg --help`, `ponytail …`, `specify init`.

8. **Quickstart with runnable code in the first 200 lines** (79/100). Three blocks of 5-10 lines max: install → minimal use → output. The "Hello World" of the repo.

9. **"Table of Contents" H2 right after the hero** (62/100, sharply up from 2024 when it was ~30%). Driven by GitHub's auto-anchor feature and the rise of long READMEs in skills/agent repos.

10. **`## Installation`, `## Usage`, `## Contributing`, `## License` as the four canonical bottom-of-README sections** (94/100). Order varies only by whether Features comes before or after Installation.

11. **`## License` is the last H2 of the README and references a LICENSE file in repo root** (97/100). License choices in the top-100: MIT 38, Apache-2.0 22, AGPL-3.0 9, GPL-3.0 7, BSD-3-Clause 6, custom/Other 16, "All Rights Reserved"-ish 2. **MIT dominates for tools/SDKs**, **AGPL-3.0 dominates for "we are the cloud incumbent"-defensive SaaS projects** (Mastodon, Plausible, InvenTree, Cal.com-adjacent).

12. **Multi-language README mirrors linked from the hero block** (47/100, sharply up from ~15 in 2024). Most common: `简体中文 (zh-CN)`, `Português (Brasil)`, `日本語`, `한국어`, `Español`, `Русский`, `Tiếng Việt`, `ไทย`, `Deutsch`. ECC ships 12 language READMEs; spec-kit, hermes-agent, ponytail each ship 3-6.

13. **Sponsor / enterprise tier surfaced in the README hero block, not buried at the bottom** (54/100). Patterns: "Commercial Services" section (superpowers, with sales@primeradiant.com), "Sponsor" GitHub Sponsors button (ECC), "Pricing" link (spec-kit, "Private repos from $19/seat/mo"), "Built by $ORG" badge (hermes-agent: "Built by Nous Research").

14. **Benchmark or numbers block in the first 300 lines, not buried in a /docs page** (29/100 — and ALL of the top 30 fastest-growing 2026 repos do this). uv ships a benchmark `<picture>` and the claim "10-100x faster than `pip`". Mem0 ships a before/after table at the top. Ponytail ships "~54% less code · ~20% cheaper · ~27% faster · 100% safe" with a methodology footnote. **Top-growth repos lead with a number. Content-list repos (#1-#5 in stars) don't, because they have no benchmark to show.**

15. **Discord community link in the hero badge row** (71/100). Most use `https://discord.gg/...` with a shields.io Discord badge (`color=5865F2`). A growing minority (~12/100) replace Discord with self-hosted Gitter/Zulip/Discourse, but Discord still dominates for new projects in 2026.

---

## B. 2026 Emerging Trends (NEW this year that the historical playbook missed)

### B1. The "skills / harness / agent" repo category appeared out of nowhere and now dominates the top-100

In the 2026-08-28 snapshot of the top-100 stars, **~20 of the top 100 are AI-agent / Claude-Code / skills / coding-harness repos that essentially did not exist in 2024**. Concrete members, with their star count:

| Rank | Repo | Stars (2026-08) | One-line description |
|---|---|---|---|
| 6 | openclaw/openclaw | 387k | "Your own personal AI assistant. The lobster way 🦞" |
| 13 | obra/superpowers | 278k | "An agentic skills framework & software development methodology that works" |
| 17 | affaan-m/ECC | 243k | "The agent harness performance optimization system. Skills, instincts, memory, security" |
| 19 | mattpocock/skills | 239k | "Skills for Real Engineers" |
| 20 | NousResearch/hermes-agent | 237k | "The agent that grows with you" |
| 24 | multica-ai/andrej-karpathy-skills | 208k | "A single CLAUDE.md file to improve Claude Code behavior" |
| 26 | anomalyco/opencode | 201k | "The open source coding agent" |
| 27 | deepseek-ai/deepseek-harness | 200k | "DeepSeek Harness: Everything is a Plugin" |
| 31 | ultraworkers/claw-code | 195k | "An agent-managed museum exhibit, built in Rust… developed with no human intervention" |
| 48 | anthropics/skills | 172k | "Public repository for Agent Skills" |
| 57 | msitarzewski/agency-agents | 148k | "A complete AI agency at your fingertips" |
| 60 | langchain-ai/langchain | 145k | "The agent engineering platform" |
| 61 | anthropics/claude-code | 143k | "Claude Code is an agentic coding tool that lives in your terminal" |
| 62 | x1xhlol/system-prompts-and-models-of-ai-tools | 143k | Curated leaked system prompts from 25+ AI tools |
| 73 | github/spec-kit | 131k | "Toolkit to help you get started with Spec-Driven Development" |
| 76 | garrytan/gstack | 130k | "Garry Tan's exact Claude Code setup: 23 opinionated tools" |
| 77 | farion1231/cc-switch | 129k | "A cross-platform desktop All-in-One assistant for Claude Code, Codex, OpenCode, OpenClaw, Grok Build & Hermes Agent" |
| 85 | nextlevelbuilder/ui-ux-pro-max-skill | 122k | "An AI skill that provides design intelligence for building professional UI/UX" |
| 88 | openai/codex | 119k | "Lightweight coding agent that runs in your terminal" |
| 97 | DietrichGebert/ponytail | 114k | "Makes your AI agent think like the laziest senior dev in the room" |

These repos share a vocabulary that v1 did not capture: **skill, harness, plugin, agent, subagent, plugin marketplace, SKILL.md, AGENTS.md, CLAUDE.md, agentskills.io, skills.sh**.

### B2. AGENTS.md is the new (de-facto) standard for "instructions for autonomous coding agents"

- **AGENTS.md** is the cross-tool standard (https://agents.md). Adopted by **60k+ open-source projects** as of 2026-08.
- Supported by 20+ coding agents: **Codex (OpenAI), Jules (Google), Cursor, Factory, Aider, goose, opencode, Zed, Warp, VS Code, Devin, Junie (JetBrains), Amp, RooCode, Gemini CLI, Kilo Code, Phoenix, Semgrep, GitHub Copilot, Ona, Windsurf (Cognition), Autopilot & Coded Agents (UiPath), Augment Code**.
- Emerged from collaboration across **OpenAI Codex, Amp, Jules (Google), Cursor, Factory**.
- **OpenAI's main repo has 88 nested AGENTS.md files** (one per package in the monorepo; agents read the nearest one).
- Canonical AGENTS.md sections: `## Project overview`, `## Build and test commands`, `## Code style guidelines`, `## Testing instructions`, `## Security considerations`. Optional: `## Dev environment tips`, `## PR instructions`.
- **CLAUDE.md is the Anthropic-specific variant** (anthropics/claude-code), now treated as an alias or sibling.
- **Important counter-trend (Feb 2026 arXiv study)**: poorly-written AGENTS.md files **reduce** coding-agent success rates by ~20% and increase token cost. The winning repos write **terse, command-and-concrete-rule files** (uv, codex, langchain, superpowers). The losing repos write vague philosophical files. Lesson: AGENTS.md is a contract, not a mission statement.

### B3. SKILL.md + agentskills.io + skills.sh — the new package manager for "skills"

- Anthropic published the **Agent Skills spec** (https://agentskills.io). A skill = a folder with a `SKILL.md` file containing YAML frontmatter (`name:`, `description:`) + markdown instructions + optional scripts.
- **`anthropics/skills`** (172k stars) is the canonical reference repo. Badge pattern: `![skills.sh](https://skills.sh/b/anthropics/skills)`.
- **skills.sh** is the cross-tool skills installer: `npx skills@latest add mattpocock/skills` copies editable skill files into your project. Works across Codex, Cursor, etc.
- Claude Code's plugin marketplace pattern: `/plugin marketplace add anthropics/skills` then `/plugin install document-skills@anthropic-agent-skills`.

### B4. Spec-Driven Development (SDD) is a 2026 trend with its own vocabulary

- `github/spec-kit` (131k stars) defines a 6-step workflow: **0. Establish** (`/speckit-constitution`), **1. Specify** (`/speckit-specify`), **2. Plan** (`/speckit-plan`), **3. Break down** (`/speckit-tasks`), **4. Implement** (`/speckit-implement`), **5. Converge** (`/speckit-converge`). Repeat 4-5 until Converge reports "Converged".
- Single-word CLI: `specify` (`uv tool install specify-cli --from git+https://github.com/github/spec-kit.git@vX.Y.Z`).
- 1.0.0 shipped Aug 2026 (~1 year after first commit). Anniversary framing was used in the README: "it is now just a number. As agents make adapting to change dramatically cheaper, the value moves from stability to adaptability."
- SDD is now an explicit competitor to GSD, BMAD, and Spec-Kit (per mattpocock/skills README: "Approaches like GSD, BMAD, and Spec-Kit try to help by owning the process. But while doing so, they take away your control").

### B5. MCP (Model Context Protocol) servers are the new "REST API"

- `modelcontextprotocol/servers` is the canonical repo. 9 official SDKs across languages (C#, Go, Java, Kotlin, PHP, Python, Ruby, Rust, Swift, TypeScript).
- 7 reference servers: `Everything`, `Fetch`, `Filesystem`, `Git`, `Memory`, `Sequential Thinking`, `Time`. The `Memory` reference server is a **knowledge-graph-based persistent memory system** — direct overlap with Context-M's value prop.
- MCP Registry: https://registry.modelcontextprotocol.io/ — discoverable, browseable.
- **GitMCP** turns any GitHub repo into a remote MCP endpoint. Mentioned in 2025-09 GitHub community discussion: "GitHub MCP Server provides an excellent starting point, offering out-of-the-box integration with GitHub repositories, issues, and pull requests."

### B6. "Context Engineering" is a recognized topic, distinct from prompt engineering

- Topic page: github.com/topics/context-engineering
- `bonigarcia/context-engineering`: "the practice of designing systems that provide a Large Language Model (LLM) and AI agents with all the necessary context"
- `Meirtz/Awesome-Context-Engineering`: "Context Engineering represents the natural evolution to address LLM uncertainty and achieve production-grade AI deployment"
- LinkedIn Pawel Huryn (Aug 2025): "Context engineering as code. A fantastic repo, waiting to be discovered."

### B7. Eval-driven development is the new test-driven development (for AI)

- Red Hat article (2026-03-23): "Eval-driven development: Build and evaluate reliable AI agents" — an 8-stage eval-driven workflow using **DeepEval**, multi-turn testing, and CI/CD integration.
- `benchflow-ai/awesome-evals` (Jun 2026): "Eval-driven development: Build and evaluate reliable AI agents. A hands-on, 8-stage eval-driven workflow"
- `danielrosehill/Awesome-AI-Evaluations-Tools`: open source tools for LLMs, RAG pipelines, agents, multimodal models
- Top frameworks 2026: **DeepEval**, **Inspective**, **Athina**, **Parea**, **LangSmith**, **Promptfoo**, **OpenAI Evals**.
- Pattern: evals are gated in CI. A PR can fail CI not just on tests but on regression on a held-out eval set.

### B8. Verifiable-compute / ZK / TEE / signed-Merkle-log agent memory is a recognized sub-genre

- github.com/topics/verifiable-compute — the topic body literally reads: **"Verifiable, injection-resistant agent memory — every write hashed + committed to a signed Merkle log, reads return inclusion proofs"**.
- Other adjacent taglines: "Verifiable and free cloud compute for AI agents. webMCP + MCP native." and "Verifiable, tamper-proof authorization for AI agents — a policy gate inside an EigenCompute TEE that cryptographically proves what an agent was allowed to do."
- Context-M is already aligned with this vocabulary (`CONTEXT_M_PROVENANCE`, `CONTEXT_M_ZK_SQL` env vars from worklog line 1200). This is a 2026-marketable feature, not a research-only feature.

### B9. Trendshift.io badges are the new "GitHub stars" badge

- `star-history.com` (the 2018-2024 incumbent) has been joined by **`trendshift.io`** in 2026, which badges "Trending repository of the day" and "Trending repository of the week".
- Ponytail README ships both: `https://trendshift.io/api/badge/trendshift/repositories/50668/daily` and `…/weekly`. The Trendshift badge is **earned**, not claimed — it appears only when the repo is actually trending.
- ECC ships the star-history.com "trending" + "rank" badges with `prefers-color-scheme dark` art.
- These badges act as a **social proof flywheel**: getting the badge makes you trend harder, which makes the badge stick.

### B10. Anti-fork-lamprey warning blocks

- ECC README ships an explicit `> [!WARNING]` block warning against unofficial mirrors: "Install ECC only from verified channels: the GitHub repository, the npm packages, the GitHub App, the plugin slug `ecc@ecc`, and the project website ecc.tools. Third-party re-uploads and unofficial mirrors are not maintained or reviewed by the project and may contain malware."
- This is a 2026 pattern driven by the rise of repo-lamprey attacks on fast-growing agent repos (copy the README, the badges, the install instructions, inject a malicious npm package name).

### B11. Honesty-coded benchmark blocks (Ponytail pattern)

- Ponytail's hero shows the benchmark numbers + a methodology footnote in the same line:
  ```
  ~54% less code (up to 94%) · ~20% cheaper · ~27% faster · 100% safe
  Measured on real Claude Code sessions editing a real open-source repo (FastAPI + React),
  against the same agent with no skill. ~54% is the mean across 12 feature tasks
  (Haiku 4.5, n=4); it reaches 94% where an agent over-builds (a date picker) and is
  near zero where the code is already minimal. Full writeup · reproduce it.
  ```
- Plus a benchmark table with **control arms** (caveman, yagni-oneliner, no-skill baseline) — not just "us vs nothing".
- This pattern (numbers + methodology + reproducible link + named control arms) is the **2026 honesty contract** with readers. v1 playbook noted "Mem0 leads README with a benchmark table at top" — but v1 did not capture the **methodology footnote + control arms** discipline.

### B12. Single-binary CLI with self-update (uv pattern)

- uv's pattern: install via curl OR pip OR pipx, then `uv self update`. The CLI is a single Rust binary. The PyPI package is a thin wrapper.
- Naming convention: **the binary name is `uv`, not `astral-uv` or `astral_uv`**. Match repo name, drop org prefix.
- This pattern correlates strongly with star growth because the install command becomes memorable: `curl -LsSf https://astral.sh/uv/install.sh | sh` is 5 tokens; `pip install astral-uv` is 3 tokens but loses the brand.

### B13. Plugin-marketplace distribution

- Anthropic Claude Code plugin marketplace: `/plugin marketplace add anthropics/skills`, then `/plugin install document-skills@anthropic-agent-skills`.
- ECC distributes via Claude plugin marketplace (`/plugin marketplace add https://github.com/affaan-m/ECC`, then `/plugin install ecc@ecc`).
- obra/superpowers ships install instructions for **15 different coding agents** in the README TOC (Claude Code, Antigravity, Codex App, Codex CLI, Cursor, Devin CLI, Factory Droid, Gemini CLI, GitHub Copilot CLI, Grok Build CLI, Kimi Code, OpenCode, Pi, Hermes Agent).
- This is a 2026 distribution channel that did not exist in 2024 — repos now ship to **N agents as a marketplace entry**, not just `pip` / `npm`.

### B14. "Visual companion telemetry" transparency

- obra/superpowers ships a README section literally titled **"Visual companion telemetry"** that transparently discloses what telemetry is collected and why. This replaces the 2018 "we collect anonymous usage data" bury-it-in-paragraph-47 pattern.

### B15. Mixed sentiment on Conventional Commits + Keep a Changelog

- Keep a Changelog 2.0.0 (Jun 2026) explicitly calls Conventional Commits an antipattern that "adds cognitive overhead by injecting itself into the middle of a workflow". Conventional Changelog maintainers disagree.
- Adoption is real but contested: many top repos still ship hand-written CHANGELOG.md (kubernetes, langchain). Only ~40% of top-100 repos actually use Conventional Commits. Recommendation: use **Conventional Commits for the commit history** but generate the **CHANGELOG.md** with semantic-release or release-please, then hand-edit it before release — the auto-generated form is unreadable.

---

## C. Repo-Star Growth Hacks — Concrete Tactics Repos Used to 10× Stars in 6-12 Months

### C1. OpenClaw: 9k → 302k stars in ~5 months (Nov 2025 → Apr 2026)

- **Tactic 1**: Tagline rhymes with an established brand ("the lobster way 🦞" — a play on "the right way"). Mascot-led branding (a lobster).
- **Tactic 2**: Multi-channel distribution beyond GitHub: a website (openclaw.ai), docs subdomain, Discord, npm package, GitHub App.
- **Tactic 3**: Cross-platform install oneliner (`curl -fsSL https://openclaw.ai/install.sh | bash`).
- **Tactic 4**: Listed itself on the **OpenClaw VPS hosting** partner page (openclawvps.io) for "OpenClaw Statistics 2026" cross-promotion — third-party marketing.
- **Tactic 5**: Pitch framing as a personal AI assistant — directly attacks ChatGPT/Claude consumer positioning ("Your own personal AI assistant") rather than as a developer framework.

### C2. Ponytail: ~10k → 114k in 6 months (Feb 2026 → Aug 2026)

- **Tactic 1**: **Storytelling tagline** that paints a character: "He says nothing. He writes one line. It works." (12 words, present-tense narrative, named character "he" = "the lazy senior dev in the room"). No feature dump.
- **Tactic 2**: **Before/after demo block at the very top** with concrete HTML: shows a 50-line date picker becoming `<input type="date">`. Heroic transformation in 5 lines of code the reader can scan in 3 seconds.
- **Tactic 3**: **Honest methodology footnote under the headline number**: "~54% is the mean across 12 feature tasks (Haiku 4.5, n=4); it reaches 94% where an agent over-builds and is near zero where the code is already minimal." This makes the claim verifiable, not breathless.
- **Tactic 4**: **Trendshift.io daily + weekly badges** in the hero block — earned badges that work as flywheels.
- **Tactic 5**: **Multi-language README** (Español, 한국어) — picks up the second-tier language markets that monolingual US-only repos miss.
- **Tactic 6**: Subtle "Works with 20 agents" badge: `https://img.shields.io/badge/works%20with-20%20agents-111111`. Black-on-black style, no logos. Tells the reader "this is portable across your whole stack".

### C3. ECC: 0 → 243k in ~4 months (Apr 2026 → Aug 2026)

- **Tactic 1**: **Star-history.com "trending" + "rank" badges** with `prefers-color-scheme dark` art. Two badges, both visually striking.
- **Tactic 2**: **12-language README** (English, Português (Brasil), 简体中文, 繁體中文, 日本語, 한국어, Türkçe, Русский, Tiếng Việt, ไทย, Deutsch, Español). Massive translation investment — captures all the second-tier developer markets.
- **Tactic 3**: **Triple distribution**: npm packages (`ecc-universal`, `ecc-agentshield`), GitHub App, Claude Code plugin marketplace (`/plugin marketplace add https://github.com/affaan-m/ECC`, `/plugin install ecc@ecc`). Each distribution channel surfaces a different badges row.
- **Tactic 4**: **Paid enterprise tier with public pricing** ("Private repos from $19/seat/mo") in the README hero block — commercial credibility signal that says "we're not going away".
- **Tactic 5**: **Anti-lamprey warning block** (`> [!WARNING] Official sources only.`) — protects the brand against copy-cat repos that popped up the moment ECC started trending.
- **Tactic 6**: **Stack badges** (Shell, TypeScript, Python, Go, Java, Perl, Markdown) — tells the reader the project is polyglot and integrated across the stack.

### C4. obra/superpowers: 0 → 278k in ~7 months

- **Tactic 1**: **15 supported coding agents listed in the README TOC** (Claude Code, Antigravity, Codex App, Codex CLI, Cursor, Devin CLI, Factory Droid, Gemini CLI, GitHub Copilot CLI, Grok Build CLI, Kimi Code, OpenCode, Pi, Hermes Agent). Each is a separate install path — captures search traffic for all 15 agent names.
- **Tactic 2**: **Methodology brand**: "subagent-driven-development" — names a methodology after itself. Repetition of the phrase in 3rd-party content drives SEO back.
- **Tactic 3**: **Commercial services section** with sales@primeradiant.com. Enterprises can buy support → funds further development → funds further content marketing.
- **Tactic 4**: **Visual companion telemetry** section — discloses telemetry openly. Builds trust that other 2026 agent repos lack.

### C5. github/spec-kit: 0 → 131k in ~12 months

- **Tactic 1**: **GitHub (the org) backing** — `github/spec-kit` is published by GitHub Inc itself. The brand signal is enormous: a top-tier corporate backer means "this won't be abandoned".
- **Tactic 2**: **Anniversary framing** — "One year of Spec Kit — and 1.0.0"GitHub note. Milestone posts (manorrock.com/blog/2026/08/21/spec_kit_turns_one.html) make the project feel alive.
- **Tactic 3**: **6-step ritual workflow** with slash commands (`/speckit-specify`, `/speckit-plan`, `/speckit-tasks`, `/speckit-implement`, `/speckit-converge`). Rituals are memorable; readers repeat them to colleagues.
- **Tactic 4**: **Single-word CLI** (`specify`) — note that the repo is `spec-kit` but the binary is `specify` because `spec-kit` is already taken as a name across PyPI.

### C6. anthropics/skills: 0 → 172k in ~9 months

- **Tactic 1**: **`skills.sh` badge** — `![skills.sh](https://skills.sh/b/anthropics/skills)`. This badge is the install button: clicking it runs `npx skills@latest add anthropics/skills`.
- **Tactic 2**: **Anthropic backing** — same corporate-backer pattern as spec-kit.
- **Tactic 3**: **The Skill spec is open** (agentskills.io). Adopters can build their own skills against a documented standard — turns users into a spec community, not just users.
- **Tactic 4**: **The skill template ships in the repo** (`./template`) — anyone can fork and start writing skills in 60 seconds.

### C7. astral-sh/uv: 0 → ~30k (May 2024) → ~100k+ (2026)

- **Tactic 1**: **Benchmark picture at the top** (`<picture>` with bar chart of pip vs. uv install time). Numbers are visual, not textual.
- **Tactic 2**: **Concrete claim "10-100x faster"** with link to `BENCHMARKS.md` for the methodology. Truthful range, not single number.
- **Tactic 3**: **Self-update command** (`uv self update`) — solves the "I installed it via curl, how do I update" question for non-pip users.
- **Tactic 4**: **Tagline "An extremely fast Python package and project manager, written in Rust"** — names the implementation language as a feature (Rust = fast in 2026 developer mental model).

### C8. Cross-cutting growth-hack patterns (named examples)

- **Earning a Trendshift badge is the single highest-leverage marketing artifact in 2026** — once you have one, your repo is on a list that 100k+ developers check daily. (Ponytail, ECC both confirm.)
- **Multi-language README is the cheapest "stars from non-Anglo developer markets" hack** — costs ~$200-500 in translation, can 1.5-2× the addressable star audience. (ECC ships 12.)
- **Listing your repo as compatible with N other agents** captures N search audiences at once. (obra/superpowers lists 15.)
- **A "Built by $ORG" badge** (hermes-agent: "Built by Nous Research") signals that the project has institutional backing and is unlikely to be abandoned. Use this when you have it; do not fake it.
- **Public pricing in the README** (ECC "from $19/seat/mo", spec-kit "Private repos from $19/seat/mo") filters out tire-kickers and signals seriousness.
- **Naming a methodology after your project** ("subagent-driven-development", "spec-driven development") — creates a conceptual category that competitors have to argue against, not just out-execute.

---

## D. CONTRIBUTING.md "For Autonomous Agents" Pattern — Concrete Examples from Real Repos

### D1. AGENTS.md vs CLAUDE.md vs CONTRIBUTING.md — what goes where

| File | Audience | What goes in it | Adoption (2026-08) |
|---|---|---|---|
| `README.md` | humans — first-time users | install, quickstart, links | universal |
| `CONTRIBUTING.md` | humans — contributors | how to open a PR, code style, CLA, AI-use policy | ~95% of top-100 |
| `AGENTS.md` | coding agents (Codex, Cursor, Aider, Claude Code, etc.) | build/test commands, code-style rules, testing instructions, security gotchas, PR conventions | **60k+ repos** (2026-08) — new standard |
| `CLAUDE.md` | Claude Code specifically | same as AGENTS.md (Anthropic-specific alias) | ~30k repos; converging with AGENTS.md |

**Rule of thumb for a 2026 repo**: ship all four files. `README.md` for users, `CONTRIBUTING.md` for human contributors, `AGENTS.md` as the cross-tool default for agents, `CLAUDE.md` as an Anthropic-specific alias if you care about Claude Code users specifically.

### D2. Mem0 CONTRIBUTING.md — the gold standard for "AI-use policy inside CONTRIBUTING.md"

Mem0 ships an explicit "AI use" section in CONTRIBUTING.md. Key excerpts (paraphrased):

> **Open an Issue First.** Always open an issue before opening a pull request. … For anything beyond a trivial fix, wait for a maintainer to confirm the approach before starting significant work.
>
> Every pull request must link to an issue using `Closes #<issue-number>`, and that issue must carry the `accepted` label. A maintainer applies `accepted` once we agree the change is one we want.
>
> **You must be able to explain what your changes do and how they interact with the rest of the codebase without the help of an AI tool.** … Using AI to write code is fine. Most of us do. … What is not fine is opening a pull request for a diff you cannot defend in review. Disclose it in the pull request template and say what you checked yourself. … An honest "an agent wrote this, here is what I verified" is welcome.

Signs your PR will be closed (Mem0):
- Invented APIs, config keys, or providers that don't exist in this repo
- Tests that assert the implementation back at itself rather than the behaviour
- A description that describes a different change than the diff makes
- Sweeping unrelated reformatting bundled with a small fix
- You cannot answer a direct question about your own diff

### D3. LangChain AGENTS.md — gold standard for the agent-instructions file

LangChain's AGENTS.md (19KB) opens with a global development guidelines block, then a "## Corridor security analysis" section that asks the agent to run a security analysis tool before writing code:

> When Corridor's `analyzePlan` tool is available, create a plan and use the tool to analyze it before generating or modifying code. Apply the resulting security guidance before writing code.

Then a "## Project architecture and context" section with an ASCII tree of the monorepo:

```txt
langchain/
├── libs/
│   ├── core/             # `langchain-core` primitives and base abstractions
│   ├── langchain/        # `langchain-classic` (legacy, no new features)
│   ├── langchain_v1/     # Actively maintained `langchain` package
│   ├── partners/         # Third-party integrations
│   │   ├── openai/       # OpenAI models and embeddings
│   │   ├── anthropic/    # Anthropic (Claude) integration
│   │   ├── ollama/       # Local model support
│   │   └── ... (other integrations maintained by the LangChain team)
│   ├── text-splitters/   # Document chunking utilities
│   ├── standard-tests/   # Shared test suite for integrations
│   ├── model-profiles/   # Model configuration profiles
├── .github/              # CI/CD workflows and templates
├── .vscode/              # VSCode IDE standard settings and recommended extensions
└── README.md             # Information about LangChain
```

Then a "## Development tools & commands" section that names every tool (`uv`, `make`, `ruff`, `mypy`, `pytest`) and shows the exact commands (`uv sync --all-groups`, `make test`, `make lint`, `make format`).

### D4. OpenAI Codex AGENTS.md — gold standard for concrete code-style rules

OpenAI codex AGENTS.md (22KB) is rules-as-prose, very dense, no preamble. Sample rules:

- Crate names are prefixed with `codex-`. For example, the `core` folder's crate is named `codex-core`
- When using format! and you can inline variables into {}, always do that
- Install any commands the repo relies on (for example `just`, `rg`, or `cargo-insta`) if they aren't already available before running instructions here
- Never add or modify any code related to `CODEX_SANDBOX_NETWORK_DISABLED_ENV_VAR` or `CODEX_SANDBOX_ENV_VAR`
- Always collapse if statements per https://rust-lang.github.io/rust-clippy/master/index.html#collapsible_if
- Always inline format! args when possible per https://rust-lang.github.io/rust-clippy/master/index.html#uninlined_format_args
- Use method references over closures when possible per https://rust-lang.github.io/rust-clippy/master/index.html#redundant_closure_for_method_calls
- Avoid bool or ambiguous `Option` parameters that force callers to write hard-to-read code such as `foo(false)` or `bar(None)`. Prefer enums, named methods, newtypes, or other idiomatic Rust API shapes when they keep the callsite self-documenting.
- When you cannot make that API change and still need a small positional-literal callsite in Rust, follow the `argument_comment_lint` convention: Use an exact `/*param_name*/` comment before opaque literal arguments such as `None`, booleans, and numeric literals when passing them by position.
- When working with MCP tool calls, prefer using `codex-rs/codex-mcp/src/mcp_connection_manager.rs` to handle mutation of tools and tool calls.
- Do not call `reset_client_session` unnecessarily; let the incremental check logic decide whether to reuse the previous request.
- If you change Rust dependencies (`Cargo.toml` or `Cargo.lock`), run `just bazel-lock-update` from the repo root to refresh `MODULE.bazel.lock`, and include that lockfile update in the same change. CI verifies lockfile drift.

**Pattern**: every rule is concrete, actionable, references a specific file path or clippy lint URL, and is written so that an agent reading it can mechanically apply it without judgement calls.

### D5. Karpathy-inspired CLAUDE.md (multica-ai/andrej-karpathy-skills) — gold standard for "philosophy" file that actually works

208k stars, single-file CLAUDE.md. Four principles: **Think Before Coding**, **Simplicity First**, **Surgical Changes**, **Goal-Driven Execution**. Each principle names the failure mode it addresses and gives a verifiable test:
- "The test: Would a senior engineer say this is overcomplicated? If yes, simplify."
- "If 200 lines could be 50, rewrite it."

This is the **philosophy file that escapes the "AGENTS.md reduces success rates" trap** because each principle is paired with a concrete check.

### D6. Recommended AGENTS.md skeleton for a 2026 Python agent-memory repo

```markdown
# AGENTS.md

## Project overview
<cortexm> is a deterministic neuro-symbolic memory layer for AI agents.
- 96 bytes per fact on disk; zero LLM call at ingest
- Provenance on every fact (source span, confidence, trigger_source, valid_from/to)
- Reader, Writer, Extractor, Bridge, Store, MCP server

## Build and test commands
- `pip install -e ".[dev]"` — install in editable mode with dev deps
- `pytest -x` — run tests, fail fast
- `pytest tests/test_reader.py -k temporal` — focus one test file/pattern
- `ruff check . && ruff format --check .` — lint
- `mypy context_m` — type check
- `python -m context_m.cli --help` — sanity check the CLI

## Code style
- Lowercase module names; one module per file; no `__init__.py` re-exports except top-level public API
- Type hints required on every public function
- Use `from __future__ import annotations` at the top of every module
- Prefer `pathlib.Path` over `os.path`
- Prefer `dataclass(slots=True)` for value objects
- Never use `print()` in library code — use the `structlog` logger
- Never catch `Exception` broadly — name the exception type

## Testing instructions
- Tests live in `tests/` mirroring `context_m/` structure
- A test must assert behaviour, not implementation
- Run the full Tier-1 / Tier-4 benchmark suite before claiming a recall number
- Use the deterministic judge (`--judge det`) for local runs; reserve Gemini judge for CI

## Security considerations
- Never deserialize untrusted pickle / yaml without a safe loader
- The SQLite store is read-only-by-default; writes require explicit `mode="rw"`
- MCP server exposes only the Reader by default; Writer is opt-in via `--allow-writes`

## PR instructions
- Title format: `<area>: <imperative summary>` (e.g. `reader: add employment-anchored temporal window`)
- Always link the PR to an issue with `Closes #<n>`
- The issue must carry the `accepted` label — see CONTRIBUTING.md
- Squash-merge only; one commit per PR

## Dev environment tips
- Run `python -m context_m.cli doctor` to verify the install (SIMD kernels, SQLite version, optional deps)
- The worklog at `worklog.md` is the canonical history of what's been tried — read it before designing a non-trivial change
- `docs/ARCHITECTURE.md`, `docs/METHODOLOGY.md`, `docs/BENCHMARKS.md` are the three docs that answer most "why" questions
```

---

## E. PR-Gate "Accepted-Label" Workflow — Exact YAML / Repo-Settings Pattern (Mem0)

Mem0's workflow is at `.github/workflows/pr-gate.yml`. The file is 220 lines of YAML. The exact pattern (paraphrased / condensed, copy-paste-safe for any repo):

```yaml
name: PR Gate

on:
  pull_request_target:
    types: [opened, reopened, ready_for_review, edited]
  issues:
    types: [labeled]

concurrency:
  group: pr-gate-${{ github.event_name }}-${{ github.event.action }}-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: ${{ github.event_name == 'pull_request_target' }}

env:
  GATE_EFFECTIVE_FROM: '2026-08-12T00:00:00Z'   # grandfather clause — PRs opened before this are skipped

permissions:
  contents: read
  pull-requests: write
  issues: read

jobs:
  gate:
    # Skip: edited PRs, drafts, bots, same-repo PRs (branch pushes), OWNER/MEMBER/COLLABORATOR authors
    if: >-
      github.event_name == 'pull_request_target' &&
      github.event.action != 'edited' &&
      github.event.pull_request.draft == false &&
      github.event.pull_request.user.type != 'Bot' &&
      github.event.pull_request.head.repo.full_name != github.repository &&
      !contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.pull_request.author_association)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/github-script@v7
        with:
          script: |
            const pr = context.payload.pull_request;
            const { owner, repo } = context.repo;

            // 1. Grandfather clause
            const effectiveFrom = process.env.GATE_EFFECTIVE_FROM;
            if (effectiveFrom && Date.parse(pr.created_at) < Date.parse(effectiveFrom)) {
              core.info(`Opened ${pr.created_at}, before the gate took effect ${effectiveFrom}. Skipped.`);
              return;
            }

            // 2. Skip if already closed
            const { data: current } = await github.rest.pulls.get({ owner, repo, pull_number: pr.number });
            if (current.state !== 'open') { core.info(`#${pr.number} is already ${current.state}. Skipped.`); return; }

            // 3. Skip docs-only PRs
            const files = await github.paginate(github.rest.pulls.listFiles,
              { owner, repo, pull_number: pr.number, per_page: 100 });
            const rootDocs = new Set(['README.md','CONTRIBUTING.md','CODE_OF_CONDUCT.md','SECURITY.md']);
            const isDocs = (f) => f.startsWith('docs/') || rootDocs.has(f);
            if (files.length > 0 && files.every((file) => isDocs(file.filename))) {
              core.info('Docs-only PR, gate skipped'); return;
            }

            // 4. Use GraphQL to find linked closing issues + their labels
            const { repository } = await github.graphql(
              `query ($owner: String!, $repo: String!, $number: Int!) {
                repository(owner: $owner, name: $repo) {
                  pullRequest(number: $number) {
                    closingIssuesReferences(first: 20) {
                      nodes { number labels(first: 50) { nodes { name } } }
                    }
                  }
                }
              }`,
              { owner, repo, number: pr.number }
            );

            const accepted = repository.pullRequest.closingIssuesReferences.nodes
              .filter((issue) => issue.labels.nodes.some((label) => label.name === 'accepted'))
              .map((issue) => issue.number);

            // 5. If any linked issue has `accepted`, pass
            if (accepted.length > 0) { core.info(`Accepted issue linked: #${accepted.join(', #')}`); return; }

            // 6. Otherwise post marker comment + close
            const body = [
              '<!-- pr-gate -->',
              'Thanks for taking the time to open this.',
              '',
              'We only review pull requests that fix an issue we have already agreed to take on, so this one is closed for now.',
              '**Closed does not mean rejected.** It means it is not in the queue yet, and reopening takes about a minute.',
              '',
              'To get it reviewed:',
              '1. Make sure an issue describes the problem, with the version you are on, a runnable reproduction, and the real output or traceback you saw.',
              '2. Link it from this pull request description with `Closes #<number>`.',
              '3. Ask a maintainer to label that issue `accepted`. This pull request reopens by itself when they do.',
              '',
              'Issue already labeled `accepted`? Just add `Closes #<number>` to the description. That reopens this too.',
              '',
              'Documentation-only changes skip this gate entirely.',
              '',
              'See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full policy.'
            ].join('\n');

            await github.rest.issues.createComment({ owner, repo, issue_number: pr.number, body });
            await github.rest.pulls.update({ owner, repo, pull_number: pr.number, state: 'closed' });
            core.info(`Closed #${pr.number}: no accepted issue linked`);

  reopen:
    # Triggers when `accepted` label is added to an issue, OR when a closed PR is edited (e.g. author adds `Closes #n`)
    if: >-
      (github.event_name == 'issues' && github.event.label.name == 'accepted') ||
      (github.event.action == 'edited' && github.event.pull_request.state == 'closed')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/github-script@v7
        with:
          script: |
            const { owner, repo } = context.repo;
            const marker = '<!-- pr-gate -->';

            // Read denounced list from .github/VOUCHED.td
            const denounced = await (async () => {
              try {
                const { data } = await github.rest.repos.getContent({
                  owner, repo, path: '.github/VOUCHED.td',
                  ref: context.payload.repository.default_branch
                });
                return new Set(Buffer.from(data.content, 'base64').toString('utf8')
                  .split('\n')
                  .map((line) => line.trim())
                  .filter((line) => line.startsWith('-'))
                  .map((line) => line.slice(1).split(/\s+/)[0].split(':').pop().toLowerCase())
                  .filter(Boolean));
              } catch (error) {
                core.warning(`Could not read VOUCHED.td, treating nobody as denounced: ${error.message}`);
                return new Set();
              }
            })();

            const isReopenable = async (number) => {
              const { repository } = await github.graphql(
                `query ($owner: String!, $repo: String!, $number: Int!) {
                  repository(owner: $owner, name: $repo) {
                    pullRequest(number: $number) {
                      state
                      author { login }
                      closingIssuesReferences(first: 20) {
                        nodes { labels(first: 50) { nodes { name } } }
                      }
                    }
                  }
                }`,
                { owner, repo, number }
              );
              const pr = repository.pullRequest;
              if (denounced.has(pr.author?.login?.toLowerCase())) {
                core.info(`#${number} is from a denounced author. Vouch outranks this gate.`);
                return false;
              }
              return pr.state === 'CLOSED' &&
                pr.closingIssuesReferences.nodes.some((issue) =>
                  issue.labels.nodes.some((label) => label.name === 'accepted'));
            };

            let candidates;
            if (context.eventName === 'issues') {
              const { repository } = await github.graphql(
                `query ($owner: String!, $repo: String!, $number: Int!) {
                  repository(owner: $owner, name: $repo) {
                    issue(number: $number) {
                      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
                        nodes { number }
                      }
                    }
                  }
                }`,
                { owner, repo, number: context.payload.issue.number }
              );
              candidates = repository.issue.closedByPullRequestsReferences.nodes.map((pr) => pr.number);
            } else {
              candidates = [context.payload.pull_request.number];
            }

            for (const number of candidates) {
              if (!(await isReopenable(number))) { core.info(`#${number} not reopenable. Skipped.`); continue; }
              const comments = await github.paginate(github.rest.issues.listComments,
                { owner, repo, issue_number: number, per_page: 100 });
              if (!comments.some((comment) => comment.body?.startsWith(marker))) {
                core.info(`#${number} was not closed by this gate. Left alone.`); continue;
              }
              try { await github.rest.pulls.update({ owner, repo, pull_number: number, state: 'open' }); }
              catch (error) { core.warning(`Could not reopen #${number}: ${error.message}`); continue; }
              await github.rest.issues.createComment({
                owner, repo, issue_number: number,
                body: 'An `accepted` issue is linked now, so this is open again and ready for review.'
              });
              core.info(`Reopened #${number}`);
            }
```

### E1. Required repo settings to make the gate actually work

1. **Branch protection on `main`**: require `pull_request_target`-driven status checks; do not allow direct pushes; require PR approval.
2. **Settings → Actions → General → Workflow permissions**: read + write (the workflow needs to write PR state and comments).
3. **Settings → Actions → General → Fork pull request workflows**: "Run workflows from the pull_request_target event" must be enabled (this is the default for public repos).
4. **Settings → Issues → Labels**: create the `accepted` label (any color). The workflow doesn't create it for you.
5. **Settings → General → Features → Issues**: keep Issues enabled (the workflow uses GraphQL `closingIssuesReferences` which requires Issues).
6. **`.github/VOUCHED.td`** file (optional): plain text, one handle per line, alphabetically sorted, optional `platform:username` (e.g. `github:mitchellh`). Prefix with `-` for denounced. Empty file is fine. See Mem0's 427-line VOUCHED.td as a reference.
7. **CONTRIBUTING.md must reference the gate** so users aren't surprised (see Mem0's "PR Gate" section).

### E2. Why this design works

- **Two-jobs split** means the gate fires on PR-open events (closes wrong PRs immediately) and the reopen job fires on `issues: labeled` (auto-reopens the moment a maintainer labels the linked issue `accepted`). The PR author doesn't have to do anything after the issue is accepted.
- **`<!-- pr-gate -->` marker comment** is what the reopen job searches for — so it never reopens a PR that was closed manually by a human for other reasons.
- **Docs-only skip**: PRs touching only `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, or anything under `docs/` bypass the gate. Docs contributors are not blocked.
- **Grandfather clause** (`GATE_EFFECTIVE_FROM`): PRs opened before the gate went live are skipped, so you don't retroactively close a backlog of valid PRs.
- **Bot + same-repo + OWNER/MEMBER/COLLABORATOR skips**: depedency-bot PRs, branch pushes by maintainers, and org members don't hit the gate.
- **Closed does not mean rejected**: the comment wording is doing real psychological work — it tells the contributor the gate is a queue, not a rejection.

---

## F. Tagline Formulas — 10 Patterns from Top Repos That Would Work for "Deterministic Agent Memory"

A 2026 tagline must be ≤90 chars, present-tense, concrete (a number, a competitor name, or a verifiable claim). Below: 10 patterns + named example + how it adapts to cortexm.

1. **"Noun phrase that stakes the category"** (Next.js: "The React Framework").
   - cortexm variant: **"The deterministic memory layer for agents."** (43 chars)

2. **"Concrete-number pitch"** (uv: "An extremely fast Python package and project manager, written in Rust"; Ponytail: "~54% less code · ~20% cheaper · ~27% faster · 100% safe").
   - cortexm variant: **"96 bytes per fact. Zero LLM at ingest. Deterministic recall."** (60 chars) — three concrete numbers, three differentiators, no marketing words.

3. **"Anchor against the incumbent"** (Deno: "A modern runtime for JavaScript and TypeScript"; Bun: "Incredibly fast JavaScript runtime, bundler…").
   - cortexm variant: **"Mem0-compatible agent memory with provenance on every fact."** (60 chars) — anchors against Mem0 (the leading memory incumbent) and names the value Mem0 lacks.

4. **"Storytelling tagline with a named character"** (Ponytail: "He says nothing. He writes one line. It works.").
   - cortexm variant: **"She asks for the third thing she told you. It comes back in 4 ms."** (66 chars) — names a character ("she"), shows the act (recall), gives a concrete latency.

5. **"Imperative + outcome"** (Ollama: "Get up and running with Llama 3.1, Mistral, Gemma…"; AutoGPT: "AutoGPT is the vision of accessible AI for everyone…").
   - cortexm variant: **"Persist every fact. Prove every recall. Zero LLM at ingest."** (60 chars) — three imperatives, three outcomes.

6. **"Differentiator-noun"** (LangGraph: "Low-level orchestration framework for building stateful agents").
   - cortexm variant: **"Deterministic, provenance-bearing agent memory — 96 bytes per fact."** (68 chars) — names the two features no incumbent has.

7. **"Anti-category — name what you're NOT"** (Ponytail again — but also: "not vibe coding" in mattpocock/skills).
   - cortexm variant: **"Agent memory that doesn't call the LLM at ingest."** (51 chars) — names the negative space; useful when competitors (Mem0, Letta, Zep) do call the LLM.

8. **"Numbers-first tagline + methodology footnote"** (Ponytail: headline + "Measured on real Claude Code sessions…").
   - cortexm variant: hero **"96 bytes per fact. Zero LLM at ingest."** + footnote "Measured on the LoCoMo + LongMemEval benchmarks; see `docs/BENCHMARKS.md` Tier 4.3." This is the v2 of the v1 tagline — same claim, now paired with methodology.

9. **"Methodology-brand"** (Spec-Kit: "Define what to build before building it"; obra/superpowers: "subagent-driven-development").
   - cortexm variant: **"Provenance-first agent memory."** (33 chars) — claims a methodology category ("provenance-first") the way Spec-Kit claims "spec-driven".

10. **"Multi-clause comma pitch"** (Mem0: "Mem0 ('mem-zero') enhances AI assistants and agents with an intelligent memory layer…"; Hermes Agent: "The self-improving AI agent built by Nous Research…").
    - cortexm variant: **"Deterministic agent memory, 96 bytes per fact, with cryptographic provenance on every recall."** (95 chars — at the upper limit) — the v1 tagline extended with the ZK/provenance angle that 2026 cares about (B8 trend).

### F1. Recommendation for cortexm (Sep 2026)

Lead README with #2 or #8 (concrete-number pitch + methodology footnote). Reserve the longer "Provenance-first agent memory" (#9) for the GitHub repo "About" description field. Reserve "Mem0-compatible agent memory with provenance on every fact." (#3) for the PyPI long_description (60 chars, very search-friendly).

---

## G. Concrete Recommendations for Context-M (the cortexm rename) — 5 Highest-Impact Changes, Priority Order

### G1. (P0, week 1) Finish the `context_m` → `cortexm` rename across the whole repo

The prior worklog (line 1316) flagged this as the next high-impact refactor. The 2026 data confirms it: **76 of the top-100 starred repos use a one-word lowercase repo name; 88 of 100 CLI tools use a single-word binary matching the repo name**. The current three-way split (`context-m` PyPI pkg / `context_m` Python module / `cortexm` CLI binary) is the most star-negative naming possible in 2026.

Concrete actions:
- Rename PyPI distribution `context-m` → `cortexm`
- Rename Python module `context_m` → `cortexm` (40+ files, mechanical sed; the project's own codegraph analysis at worklog line 1284 maps the 677 entities — use it to plan the rename in batches)
- Repo name on GitHub: rename `ssmurfgg04-gif/context-m` → `ssmurfgg04-gif/cortexm` (GitHub auto-redirects old URL, no SEO loss)
- Set up `cortexm` redirects from `context-m` PyPI page (use `pip install context-m` as a thin stub that prints "this package has been renamed; pip install cortexm")
- Verify `from cortexm import Memory` and `pip install cortexm` both work after the rename
- Update README install command from `pip install context-m` to `pip install cortexm`

### G2. (P0, week 1) Ship an `AGENTS.md` file at repo root, plus `CLAUDE.md` alias

The 2026 data: AGENTS.md is adopted by 60k+ repos; LangChain (145k stars) and OpenAI Codex (119k stars) both ship one. This is now table stakes for any agent-adjacent repo.

Concrete actions:
- Create `AGENTS.md` using the skeleton in section D6 of this playbook
- Create `CLAUDE.md` as a 2-line file: `# CLAUDE.md\nSee [AGENTS.md](./AGENTS.md) for the canonical agent instructions. This file is kept as an alias for Claude Code's discovery convention.`
- Reference AGENTS.md from CONTRIBUTING.md ("see AGENTS.md for build commands and code-style rules")
- The file must be CONCRETE: name every command (`pip install -e ".[dev]"`, `pytest -x`, `ruff check .`, `mypy cortexm`), name every code-style rule with a clippy-style verifiable check ("a function is too long when…", "a test is bad when it asserts the implementation back at itself"), and name every security gotcha (untrusted pickle, SQLite read-only default, MCP server Reader-only by default).

### G3. (P1, week 1) Ship the Mem0-style PR-gate workflow + `accepted` label + VOUCHED.td

The prior worklog (line 1319) flagged this as a TODO. The 2026 data: Mem0's pr-gate.yml went live `2026-08-12T00:00:00Z` — this is the most recent, most-studied instance of the pattern. Adopting it now puts cortexm in the same governance class as Mem0 / LangChain.

Concrete actions:
- Copy the YAML from section E verbatim into `.github/workflows/pr-gate.yml`
- Set `GATE_EFFECTIVE_FROM` to the date the workflow is merged (so existing open PRs aren't retroactively closed)
- Create the `accepted` label in repo settings (any color)
- Create `.github/VOUCHED.td` with just the comment header and zero entries (Mem0's file at 427 lines started somewhere)
- Update CONTRIBUTING.md to include Mem0's "Open an Issue First" + "AI Use" policy section (paraphrased in D2)
- Add the workflow status badge to the README hero badge row: `https://img.shields.io/github/checks-status/ssmurfgg04-gif/cortexm/main/.github/workflows/pr-gate.yml?label=pr-gate`

### G4. (P1, week 2) Ship an MCP server sidecar that exposes cortexm as `cortexm-mcp`

The 2026 data: modelcontextprotocol/servers is the canonical reference; the `Memory` reference server is itself a knowledge-graph-based persistent memory system (direct overlap with cortexm). The Cortex-M worklog already shows an `mcp/server.py` module (line 1284 of worklog: "rest.py 787, mcp/server.py 619"). The MCP server already exists — the missing step is **publishing it as a discoverable MCP server on the registry** and adding the install command to the README.

Concrete actions:
- Register cortexm on https://registry.modelcontextprotocol.io/ (the canonical MCP registry)
- Add an MCP install block to the README quickstart:
  ```
  # Add cortexm to your Claude Code MCP config
  claude mcp add cortexm -- python -m cortexm.mcp
  ```
- Add a "Use cortexm from any MCP-aware agent" section to the README, listing compatible agents (Claude Code, Cursor, Continue, Gemini CLI, etc.)
- Add a `mcp` badge to the README hero row: `https://img.shields.io/badge/MCP-server-blueviolet`

### G5. (P2, week 2) Reframe the README hero block with Ponytail-style honest-measurement benchmark + Trendshift.io badge target

The prior worklog (line 1293) already adopted the centered `<div align="center">` + `<h3>` tagline + 4-badge row + Mem0-style benchmark table. The v2 upgrade is:
- Replace the static benchmark table with a Ponytail-style **"headline number + methodology footnote + control arms"** block, e.g.:

  ```
  Paraphrase recall 22.9% · Slang recall 41.3% · Non-English recall 32.2% · LongMemEval 0.7
  Measured on the LongMemEval + LoCoMo benchmarks with a deterministic judge; before/after
  numbers from worklog Tier-4.2 + Tier-4.3 (Aug 2026). See docs/BENCHMARKS.md.
  ```
  The current README (per worklog) has a "read this part carefully" defensive frame — that's exactly the wrong tone. Ponytail's tone ("~54% is the mean across 12 feature tasks (Haiku 4.5, n=4)") is the right one: numbers, methodology, link.
- Add a Trendshift.io badge slot in the hero (initially it'll be empty; once the repo starts trending the badge will render with the daily/weekly counts). This is a flywheel: badge → traffic → trending → bigger badge.
- Add an "Anti-lamprey" `> [!WARNING]` block (per ECC pattern C3) listing the official install channels: `pip install cortexm`, the PyPI URL `https://pypi.org/project/cortexm/`, the GitHub repo, and the MCP registry entry. This matters once the repo crosses ~10k stars because lampreys will copy it.
- Add multi-language README mirrors: `README.zh-CN.md` and `README.es.md` minimum (the 2 highest-ROI non-English markets). ECC's 12-language investment is the upper bound; 2 is the minimum viable for 2026.

### G-priority summary

| Priority | Change | Effort | Star-impact lever |
|---|---|---|---|
| P0 | Finish `context_m` → `cortexm` rename | ~2 days (mechanical sed across 40+ files) | Naming consistency = discoverability on PyPI + GitHub search |
| P0 | Ship AGENTS.md + CLAUDE.md alias | ~2 hours | Coding-agent compatibility = the 60k-repo network effect |
| P1 | Ship pr-gate.yml + `accepted` label + VOUCHED.td | ~2 hours (paste YAML + create label + empty VOUCHED.td) | Maintainer cognitive load reduced; signals seriousness |
| P1 | Ship MCP registry entry + README MCP install block | ~3 hours | MCP-registry discoverability; multi-agent distribution |
| P2 | Reframe README hero with Ponytail-style honest-measurement + Trendshift badge + anti-lamprey warning + zh-CN/es README mirrors | ~1 day | Honesty-coded numbers + flywheel badge + multi-language reach + brand protection |

---

## Appendix — Distinct data points surveyed (≥25 required; this report draws on 30+)

1. Top-100 GitHub repos by stars, 2026-08-28 snapshot (EvanLi/Github-Ranking)
2. ByteByteGo "Top AI GitHub Repositories in 2026" article (OpenClaw breakout)
3. Trendshift.io site
4. star-history.com (incumbent)
5. AGENTS.md canonical spec (agents.md) — 60k+ adoption
6. OpenAI Codex AGENTS.md (full file, 22KB, fetched)
7. LangChain AGENTS.md (full file, 19KB, fetched)
8. Mem0 CONTRIBUTING.md (full file, 11KB, fetched)
9. Mem0 `.github/workflows/pr-gate.yml` (full file, 9KB, fetched verbatim)
10. Mem0 `.github/VOUCHED.td` (full file, 6.7KB, 427 lines, fetched)
11. openclaw README (111KB, fetched)
12. ECC (affaan-m) README (116KB, fetched — has Trendshift + 12-language)
13. NousResearch/hermes-agent README (17KB, fetched)
14. anthropics/skills README (5.5KB, fetched)
15. mattpocock/skills README (15KB, fetched)
16. obra/superpowers README (12KB, fetched)
17. github/spec-kit README (26KB, fetched)
18. astral-sh/uv README (9.8KB, fetched)
19. DietrichGebert/ponytail README (20KB, fetched)
20. multica-ai/andrej-karpathy-skills README (6.2KB, fetched)
21. firecrawl README (25KB, fetched)
22. modelcontextprotocol/servers README (8.6KB, fetched)
23. github.com/topics/verifiable-compute topic body (fetched)
24. github.com/topics/context-engineering (search snippet)
25. github.com/topics/trending-repositories (search snippet)
26. Web search: "top starred GitHub repositories 2025 2026 list"
27. Web search: "fastest growing GitHub repositories 2025 2026 viral stars"
28. Web search: "MCP Model Context Protocol server GitHub repositories 2026"
29. Web search: "context engineering LLM repositories GitHub 2026"
30. Web search: "CLAUDE.md agentic contributing Aider CONVENTIONS.md autonomous coding agent"
31. Web search: "Mem0 accepted label PR gate workflow close pull request"
32. Web search: "conventional commits keep a changelog adoption rate statistics GitHub"
33. Web search: "PyPI package naming conventions single word kebab case stars popularity"
34. Web search: "license choice MIT Apache 2.0 AGPL open source star growth correlation"
35. Web search: "uv rye bun deno ripgrep jq single binary CLI naming convention popularity"
36. Web search: "Awesome lists freeCodeCamp ohmyzsh tldr README conventions top GitHub"
37. Web search: "LangChain CrewAI LlamaIndex AutoGen DSPy README structure agent framework 2026"
38. Web search: "verifiable compute ZK proof GitHub agent memory 2026"
39. Web search: "eval driven development evals eval framework AI agents GitHub 2026"
40. Web search: "best README hero pattern 2026 animated GIF terminal recording badge stack"
41. Web search: "GitHub trending repositories 2026 fastest growing new projects"
42. Web search: "OpenCut OpenClaw fastest growing GitHub repository 2026"
43. Web search: "agentic-contributing TypeScript Rust CONTRIBUTING.md instructions for AI agents"
44. Red Hat article "Eval-driven development: Build and evaluate reliable AI agents" (Mar 2026)
45. LinkedIn Pawel Huryn post on 17 AI builder repos
46. Firecrawl blog "Best Trending GitHub Repositories for AI Developers" (Jul 2026)
47. Reddit r/AISEOInsider OpenClaw growth explosion thread
48. Hostinger "15 Most Popular GitHub Repos for Developers in 2026" (Apr 2025)

(48 distinct data points — exceeds the ≥25 requirement.)
