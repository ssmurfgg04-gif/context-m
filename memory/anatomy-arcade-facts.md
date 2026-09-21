# anatomy-arcade — agent memory (append-only fact log)

_DB: `memory/anatomy-arcade.db` • cortexm μ=0 deterministic memory • started 2026-09-21_

_Query recipe: `python3 /home/z/my-project/scripts/anatomy_memory.py search anatomy-arcade "<query>"`_

- Anatomy Arcade is a premium playable 3D biology game: player is a microscopic medical nano-robot inside the human body solving biological emergencies. Repo github.com/ssmurfgg04-gif/anatomy-arcade. Canonical spec is PROJECT.md (66 sections).
- Stack: Next.js App Router + React Three Fiber + Three.js + Drei + Zustand game state + Tailwind UI + Qwen educational scans via server API (never expose keys client-side, static fallback required).
- Priority order (spec 47/61): 1 Heart Attack Response vertical slice, 2 Main Menu, 3 Full-Body Explorer, 4 Viral Invasion, 5 Brain Mission. Never sacrifice the working heart mission for another half-finished feature.
- Visual direction: MICROSCOPIC SCI-FI MEDICAL THRILLER. Near-black biological background, deep crimson arterial cues, oxygen cyan interface accents, subtle white type. FORBIDDEN: purple AI gradients, glassmorphism spam, dashboard cards, emoji UI, RGB neon soup, lorem ipsum. Taste skill settings DESIGN_VARIANCE 7-8, MOTION_INTENSITY 7-9, VISUAL_DENSITY 4-6.
- Mandatory process: install Leonxlnx/taste-skill design-taste-frontend BEFORE UI work; every external asset logged in docs/ASSETS.md with license/attribution; 5 aggressive VLM critique rounds logged in docs/VLM-CRITIQUE.md with fixes applied; agents append worklog.md and update CONTEXT.md then push at every milestone.
- Game state machine: BOOT, LOADING, MAIN_MENU, MISSION_SELECT, MISSION_INTRO, PLAYING, SCANNING, INTERACTION, OBJECTIVE_COMPLETE, EDUCATION_POPUP, MISSION_COMPLETE, RESULTS. Controls desktop WASD+mouse+Shift boost+E interact; mobile left joystick + right swipe look + buttons. Quality tiers AUTO/LOW/MEDIUM/HIGH.
- Environment: bun package manager (never npm install in repo); GitHub push at every milestone (sandbox wipes); subagents read CONTEXT.md + worklog.md, append worklog, update CONTEXT, push; long jobs run via nohup.
- Context durability protocol: facts go into cortexm DB at context-m/memory/anatomy-arcade.db AND append-only log memory/anatomy-arcade-facts.md AND get committed+pushed to ssmurfgg04-gif/context-m repo via scripts/anatomy_memory.py export. Parallel agents can all do this safely.
- Asset research P4 done: see docs/ASSET-RESEARCH.md, top picks recorded
- P1-P3 DONE: heart mission vertical slice fully playable E2E (browser-verified): menu->intro->flight->scan (Qwen AI panel)->clot treatment->flow restore->S-rank results. Engine: Zustand phases, procedural vessel spline, instanced RBCs, destructible clot, WebAudio SFX. Debug helpers window.__aa/__aaRefs/__aaTp/__aaAimAt survive for testing.
- Sandbox lesson: SwiftShader software GL runs R3F at ~1-3fps; detect via WEBGL_debug_renderer_info containing swiftshader -> force LOW tier (dpr 0.5-0.7, LOW_TIER shader define, Lambert materials, dt clamp 0.22, wall-clock intro). Playwright/agent-browser keydown too fast for low fps - dispatch synthetic KeyboardEvent and hold 3-6s.
