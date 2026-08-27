# context-m-claude

**Your Claude Code agent now remembers across every repo, every session,
every team — with cryptographic audit trails and 96 bytes per memory.**

A Claude Code extension that replaces flat-file session memory with the
[Context-M](../../) Memory Fabric: bi-temporal knowledge graph +
holographic vector memory, μ=0 ingest (zero LLM calls), deterministic
provenance on every retrieval.

## Why not `claude-mem`?

| | claude-mem | context-m-claude |
|---|---|---|
| Storage | local JSON file | bi-temporal knowledge graph + VSA palace |
| Memory ops | append-only | contradiction resolution, supersessions, blame |
| Audit | none | query → VSA match → symbolic dereference → BLAKE3 hash → source text |
| Temporal | none | `get_between/before/after` on valid-time and transaction-time |
| Security | none | InjecMEM quarantine, scope sandboxing, ZK-lite proofs |
| Edge | no | offline, 96 B/vector — 10M memories on a Raspberry Pi 5 |
| Cost | LLM extraction per write | μ=0 — deterministic, ~$10 per million memories |

## Install

```bash
pip install cortexm            # the fabric (one command, works offline)
npm install && npm run build   # this plugin
```

Add to `.claude/settings.json` (or Claude Desktop config):

```json
{
  "mcpServers": {
    "context-m": {
      "command": "cortexm",
      "args": ["serve"],
      "env": { "CONTEXT_M_DB": "~/.context-m/memory.db" }
    }
  }
}
```

## Use

```ts
import { onUserTurn, onAssistantTurn } from "context-m-claude";

// pre-turn: inject relevant memories as context
const memoryBlock = await onUserTurn("refactor the payment service auth");

// post-turn: store new facts (μ=0, no LLM calls)
await onAssistantTurn([{ role: "user", "content": "we use Rust now" }]);
```

## Tools exposed

- `contextm_add` — store memories from conversations
- `contextm_search` — neuro-symbolic retrieval with provenance
- `contextm_temporal` — before / after / between queries
- `contextm_audit` — the full "Why" chain for any retrieval
- `contextm_prove` — ZK-lite proof (match verified, content redacted)
- `contextm_stats` / `contextm_get_all` / `contextm_history` / `contextm_delete`
