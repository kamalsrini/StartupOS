---
slice: decisions
updated: 2026-09-04
source: decision
---
# Decision log

- **2026-04-03 — GTM moves to the Azure VM.** Outbound (Apollo sequences, scoring, attribution merge) runs from the Azure GTM scripts rather than a laptop. CRA demo commit deployed the same day (one failed deploy, re-push succeeded 14 minutes later).
- **2026-05-27 — StartupOS is memory-centric.** The company brain (Markdown repo + Postgres) is the product; agents read from it and write decisions back through approvals.
- **2026-09-03 — Demo build.** The Cockpit artifact (Build, Marketing, Web, Customers, Finance, Sales modules with MODULE_SNAPSHOTS / BREX_SNAPSHOT / EXEC) is the reference UI; the API must serve those shapes unchanged.
- **2026-09-04 — Independent runtime.** StartupOS moves off the Cowork artifact runtime to a self-hosted service: brain (Postgres + brain/ repo), deterministic ingest and signal engine, one Claude Agent SDK daemon, approval gate, FastAPI + Next.js. No model in ingest, polling, or rendering; nightly context pack with prompt caching; three compute tiers; per-tenant budgets that degrade instead of overbilling. Target token COGS $8–20/month per Founder seat.
