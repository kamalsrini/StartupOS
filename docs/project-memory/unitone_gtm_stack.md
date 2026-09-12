---
name: unitone-gtm-stack
description: "UnitOne GTM is a production outbound stack running on Azure VM. It is the substrate for the StartupOS Sales/Outbound module — don't rebuild it, wrap it."
type: project
---

**The Sales module of StartupOS is built on top of UnitOne GTM, an already-live production outbound stack.** Don't rebuild it. Wrap it.

**Runtime:**
- Azure VM: `azureuser@20.106.244.178` (VM name: `autonomousSMB`)
- OS: Ubuntu 22.04.5 LTS, Python 3, Claude Code installed
- Project dir: `~/unitone-gtm/`
- `.env` with `APOLLO_API_KEY` + `SLACK_WEBHOOK_URL` (chmod 600)
- `cron_runner.sh` sources `.env`, runs scripts, logs to `~/unitone-gtm/logs/`, rotates 30 days
- Reports: `~/unitone-gtm/reports/unitone_outbound_report.xlsx`

**Why Azure VM instead of Cowork scheduled tasks:** Claude sandbox cannot reach apollo.io or hooks.slack.com (proxy 403). Migrated 2026-04-03.

**7 cron jobs running M-F (UTC times shown, EDT in parens):**
| UTC | ET | Script | Purpose |
|---|---|---|---|
| 11:00 | 7am | auto_enroll.py | Net-new enrollment, cap 25/day, dedup |
| 11:30 | 7:30am | daily_report.py | Apollo pull → Excel + Slack summary |
| 12:00 | 8am | engagement_watcher.py | Morning engagement check + Slack alerts |
| 13:00 | 9am | linkedin_followup_queue.py | Engagement → LinkedIn DM queue CSV |
| 16:00 | 12pm | engagement_watcher.py | Midday check |
| 21:00 | 5pm | engagement_watcher.py --health | EOD check + health report |
| 21:00 Fri | 5pm Fri | attribution_merge.py | Weekly PostHog + Apollo attribution |

**Slack alert taxonomy (engagement_watcher.py 3×/day):**
- 🔴 REPLY DETECTED (highest)
- 🟠 CLICK DETECTED (24hr LinkedIn DM)
- 🟡 MULTI-OPEN 3+ (send connection)
- ⚪ BOUNCE/UNSUB (informational, auto-tracked in watcher_state.json)
- 📊 SEQUENCE HEALTH REPORT (5pm daily)

**Apollo sequences as of last snapshot (2026-04-03):**
- VP Engineering — 42 contacts
- CTO v2 — 36 contacts
- CRA OT/BAS — 763 contacts (Emerson, Schneider, Honeywell, Siemens + 6 more)
- LinkedIn follow-up: 79 generated, 18/79 sent manually
- Total: 841 contacts across 3 sequences

**Why:** Productionizing outbound was the user's first agent before StartupOS. It's working — Sequence VP Eng hit 27% open rate, 13.5% bounce rate; CRA OT/BAS launched 2026-04-03 to 763 contacts. This is the existence proof that the StartupOS execution thesis works.

**How to apply:** When building the StartupOS Sales module, source data from this stack rather than from scratch. The artifact should: (1) call Apollo MCP directly for live sequence stats (already authorized in this account), (2) eventually read engagement reports from the Azure VM via SCP or a small read API, (3) visualize what's already happening, (4) trigger the 9 `gtm-engine` skills as approve-before-execute actions. Don't duplicate auto_enroll/engagement_watcher logic — they're already running on cron.

**Operations playbook:**
```bash
ssh azureuser@20.106.244.178
crontab -l                                    # check cron jobs
ls -la ~/unitone-gtm/logs/                    # today's logs
grep "$(date +%Y-%m-%d)" ~/unitone-gtm/logs/*.log
cd ~/unitone-gtm && source .env && python3 engagement_watcher.py --health
scp azureuser@20.106.244.178:~/unitone-gtm/reports/unitone_outbound_report.xlsx .
```

Related: [[unitone-gtm-scripts]] for per-script detail.
