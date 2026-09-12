---
name: startupos-sdlc
description: How the user wants every StartupOS build session run — three hats (Principal Engineer, Dir of Product, Dir of Test), gated SDLC, parallel agents, briefs read first. Read at the start of ANY StartupOS work.
type: feedback
---

**Rule (stated 2026-09-04):** Run StartupOS work as an SDLC pipeline with as many parallel agents as useful. Always: (1) read the Research Brief and the Architecture Brief first; (2) start with a set of tasks; (3) act as Principal Engineer reviewing the project at all times; (4) act as Director of Product keeping tasks, reviews and functionality in check; (5) act as Director of Test — lint, functional, unit and regression must pass before moving to the next task. Codebase lives in `/Users/kamal/StartupAgents/startupos/` (local git, no GitHub yet). User will provide Linear, Slack and Brex API keys via `.env` on the Mac.

Why: the user wants the process to be automatic so it never has to be re-explained across the project.

How to apply: invoke the `startupos-sdlc` skill (proposed 2026-09-04) at session start; if it isn't saved, follow this file. Keep sprint state in [[startupos-sdlc-state]]. Gates: `make check` = ruff + pytest unit/functional/regression + web lint. Never close a task with a red gate. Report gaps openly.
