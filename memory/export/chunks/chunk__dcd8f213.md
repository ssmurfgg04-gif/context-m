---
id: dcd8f2135ee74d76ad8e901a2a6490ba
user_id: anatomy-arcade
agent_id: null
run_id: null
created_at: 
hash: 30e6811ef9783160b7dd579880769e9318ab73f9dab56fd11e87da55316e41ad
---

Context durability protocol: facts go into cortexm DB at context-m/memory/anatomy-arcade.db AND append-only log memory/anatomy-arcade-facts.md AND get committed+pushed to ssmurfgg04-gif/context-m repo via scripts/anatomy_memory.py export. Parallel agents can all do this safely.