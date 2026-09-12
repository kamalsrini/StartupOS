---
name: unitone-gtm-scripts
description: "Per-script inventory of the UnitOne GTM Python stack — what each does, where it runs, what it outputs."
type: reference
---

All scripts live in `~/unitone-gtm/` on the Azure VM (see [[unitone-gtm-stack]]).

**Signal & scoring layer (run on demand or weekly):**
- `signals.py` — GitHub API + HN Algolia + job postings + funding news → raw signals CSV
- `scorer.py` — Firmographic (0-40) + Technographic (0-30) + Intent (0-30) → tiered accounts
- `messenger.py` — Persona templates (VP Eng, CTO, CFO, CEO). Template-based, no AI credits.
- `pipeline.py` — Orchestrates signals → scorer → messenger. Outputs CSVs per run.

**Enrollment layer:**
- `auto_enroll.py` — Idempotent enrollment. Dedup against active contacts. Daily cap 25, batch 10. Cron 7am ET M-F.
- `apollo_enroll_contacts.py` — Direct batch API enrollment helper.

**Monitoring layer (Slack-alerted):**
- `engagement_watcher.py` — Polls Apollo 3×/day (8am/12pm/5pm ET). Fires 🔴 reply / 🟠 click / 🟡 multi-open / ⚪ bounce alerts. `--health` flag at 5pm fires 📊 health report. Writes to `watcher_state.json`.
- `daily_report.py` — Full Apollo pull → Excel snapshot to `~/unitone-gtm/reports/` + Slack summary. 7:30am ET.
- `linkedin_followup_queue.py` — Reads engagement triggers → emits personalized LinkedIn connect notes + DM queue CSV. 9am ET.
- `engagement_puller.py` — Foundation: pulls Apollo + PostHog into normalized CSVs. Used by watcher + merge.
- `content_engagement_monitor.py` — Weekly content × channel × company performance + multi-channel company detection. Slack summary.

**Attribution layer:**
- `attribution_merge.py` — Fridays 5pm. PostHog + Apollo → per-contact multi-channel attribution with diversity scoring.

**Content distribution:**
- `content_distributor.py` — UTM-tagged links across 6 channels per content piece → distribution checklist + CSV.
- `github_traffic_puller.py` — GitHub `/traffic/views`, `/clones`, `/referrers` → CSVs + ICP domain matching.
- `inject_posthog.py` — Analytics injection helper.
- `push_attribution_to_github.sh` — Deploy attribution dashboards to Vercel.

**PostHog deployment:** Cookieless mode, deployed 2026-04-01. UTMs in all Apollo sequences per `UTM_Taxonomy.md`.

**Channels instrumented (5):** Email (Apollo), LinkedIn (post + DM), Substack `@notabotkamal`, `unitone.ai/blog`, GitHub `SecuritySkills`.

**Landing pages live on Vercel:** 25 Teardown pages (CTO persona, HUD/sci-fi aesthetic), 25 ROI pages (CFO/CEO), general demo, CRA demo (OT/BAS Niagara ecosystem).

**Not yet built (Sprint 3 work):** `score_recalibrator.py`, `message_optimizer.py`, `weekly_digest.py`, `wave_planner.py`, `content_planner.py` — the feedback loop layer.

**Source of truth for this layout:** the Architecture HTML the user pasted in chat on 2026-05-27 (Outputs > "Gtm autopilot architecture").
