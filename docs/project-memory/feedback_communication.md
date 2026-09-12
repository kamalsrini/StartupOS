---
name: feedback-communication
description: "How the user prefers Claude to work in this project — research before building when scope is broad, ask before going deep, surface gaps don't hide them."
type: feedback
---

**Rule 1 — When scope is broad, research before building.**
- **Why:** When asked "build an interface to manage my startups," user picked "Research first, then decide" rather than jump to a prototype. They want a positioning thesis before pixels.
- **How to apply:** For any request that introduces a new product, module, or major capability, do competitive research and synthesize a build recommendation before opening a canvas. For small additions to existing work, skip straight to build.

**Rule 2 — Ask clarifying questions up front using AskUserQuestion, not in prose.**
- **Why:** User responds quickly and decisively to multiple-choice. Open-ended "what would you like?" stalls them.
- **How to apply:** Lead with 1–3 AskUserQuestion blocks before any non-trivial work. Recommend an option (mark "Recommended" in label).

**Rule 3 — Surface gaps, don't hide them.**
- **Why:** When fetches failed or context was missing during research, user wanted that called out (not papered over with assumed answers). Same pattern in their own GTM stack — they instrument bounces and surface them, don't suppress.
- **How to apply:** When a tool errors, a connector is missing, or context is incomplete, say so explicitly in the answer. Don't simulate or fake outputs to look complete.

**Rule 4 — Use persistent surfaces (artifacts, docs, memory) over chat outputs.**
- **Why:** User explicitly said the project reference MD should be written-with across sessions, not regenerated.
- **How to apply:** Default to artifact / file / memory when the output has any shelf life. Chat is for synthesis and decisions.
