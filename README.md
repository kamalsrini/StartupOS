# StartupAgents

Working folder for **StartupOS** — the memory-centric founder operating system ("opens with work already done") — and the UnitOne GTM material that feeds it.

| Path | What it is |
|---|---|
| `StartupOS_Research_Brief.md` | Canonical product brief (thesis, competitive landscape, IA, pricing, phased plan). Written-with across sessions. |
| `StartupOS_Architecture_Brief.md` | Canonical architecture brief. Read both briefs before any StartupOS work. |
| `startupos/` | The StartupOS service (Python API/daemon/ingest + Next.js web, Postgres/pgvector, docker compose). Has its own README, `Makefile` (`make check` = gates) and `.env.example`. |
| `startupos_sales_module.html` | Single-file demo artifact (baked MCP snapshots + live artifact-runtime bridge). |
| `campaigns/` | Outbound campaign drafts (brief / targets / sequence / landing-page spec). |
| `docs/project-memory/` | Export of the Cowork project memory so context travels with the repo. Start at `MEMORY.md`. |

## Setting up on another machine

```bash
git clone <this repo> ~/StartupAgents
cp ~/StartupAgents/startupos/.env.example ~/StartupAgents/startupos/.env   # fill in keys
```

Then link `~/StartupAgents` as a Cowork project and point Claude at `docs/project-memory/MEMORY.md` to re-seed project memory. Secrets (`.env`) are never committed.
