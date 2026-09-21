---
id: 3bbc7be848454f458e3fa7f0fe95c6dd
user_id: anatomy-arcade
agent_id: null
run_id: null
created_at: 
hash: ed0852259a9025edaaa9c7adce8f95a6e7eb49318c3691cc2f4c511360526b68
---

Sandbox lesson: SwiftShader software GL runs R3F at ~1-3fps; detect via WEBGL_debug_renderer_info containing swiftshader -> force LOW tier (dpr 0.5-0.7, LOW_TIER shader define, Lambert materials, dt clamp 0.22, wall-clock intro). Playwright/agent-browser keydown too fast for low fps - dispatch synthetic KeyboardEvent and hold 3-6s.