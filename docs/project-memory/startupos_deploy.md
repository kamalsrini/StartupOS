---
name: startupos-deploy
description: Where StartupOS runs in production — Azure VM docker compose deployment, how to update, inspect, and stop it. Read for any "is it running / restart / deploy / logs" question.
type: reference
---

**Deployed 2026-09-04 (user ran `./scripts/deploy_azure.sh` from the Mac).** Host: `azureuser@20.106.244.178`, dir `~/startupos`, Docker Compose services: `db` (pgvector/pgvector:pg16, port 5432, healthy), `migrate` (applies schema, exits 0), `ingest` (`python -m ingest.runner --loop`), `daemon` (`python -m daemon.main`), `api` (uvicorn, `0.0.0.0:8000`). `.env` on the VM is rsynced from `StartupAgents/startupos/.env`.

**Operate (ssh from the Mac; the session sandbox has no SSH key):**
- update: re-run `./scripts/deploy_azure.sh` (rsync + `docker compose up -d --build`)
- logs: `cd ~/startupos && sudo docker compose logs -f daemon|ingest|api`
- digest now: `sudo docker compose exec api python -m signals.digest`
- API: `curl localhost:8000/cockpit` on the VM; port 8000 is NOT open in the NSG (keep it closed until auth ships — Sprint 2)
- stop: `sudo docker compose down`

**Caveats at deploy time:** API had no auth/tenant isolation (Sprint 2 in progress); Slack/Brex keys empty → those sources skipped; Vercel token personal-scope → 403. Compose runs `migrate` with the superuser DSN; services should move to `STARTUPOS_APP_DSN` (RLS role) once Sprint 2 lands — update `.env` on the VM and redeploy.
