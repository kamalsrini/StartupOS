---
name: startupos-artifact-runtime
description: How the StartupOS artifact actually runs — no local server, baked MCP snapshots + live artifact-runtime bridge (claude.use('mcp')), published artifact URL, connector manifest. Read when the user asks to "start the server", demo StartupOS, or refresh module data.
type: project
---

**There is no local server or separate frontend/backend repo for the StartupOS demo artifact.** The whole demo is one file: `StartupAgents/startupos_sales_module.html` (~165 KB, single-file HTML, Chart.js from jsdelivr, memory in localStorage). Confirmed 2026-09-03 — the user remembered "a frontend, backend and a Brex snapshot loop"; that maps to: artifact = frontend, MCP calls from the page (Brex/Apollo/PostHog) + Azure VM cron stack = backend, `refresh-brex-snapshot` scheduled task = the loop (no longer registered anywhere; last snapshot before this session was 2026-06-05). (The `startupos/` Python service is the separate Sprint 1–2 backend — see startupos_sdlc_state.md.)

**Published artifact (stable URL):** https://claude.ai/code/artifact/d4b69e30-a93a-4712-9f2a-cf1eadf121e7 — contract 0.2.41, capabilities `mcp`: Brex[get_user_myself, list_business_accounts, list_bills, list_vendors, list_cards, list_expenses, list_banking_transactions], Linear[list_issues, list_projects, save_issue], Vercel[list_deployments]. Republish from the source minus `<!DOCTYPE>/<html>/<head>/<body>` wrappers, or pass the URL as `url` from another session. Omit `capabilities` on redeploy to keep the manifest.

**Live bridge (2026-09-03, `live.js` block in the file):** `getMcp()` resolves `claude.use('mcp')`; `mcpCall(server, tool, input)` returns `result.payload`. On resolve: `loadBrexFinance()` (Finance → ● live) and `loadBuildLive()` (Linear list_issues limit 100 + Vercel list_deployments ×3 → tiles/signals/deploy table). Approvals in `EXEC` map execute via Linear `save_issue` on Approve (assign-158, jci-160, prio-153, jci-159, merge-150, triage-jci); result text + issue URL rendered inline. Every other approval just records. Error copy branches on McpError code (`mcpErrorCopy`). Outside the artifact runtime the page silently stays on snapshots; inside Cowork's old panel the `window.cowork.callMcpTool` path still works for Brex.
- Observed shapes: Brex `balance_breakdown.available_balance` and transaction `amount` are `{value,currency,display}` objects on the live API (snapshot uses strings) — `parseMoneyStr` handles both. Linear `save_issue` returns the full issue object (`id` = identifier like UNI-162, `url`, `assignee` name, `dueDate`, `priority.{value,name}`).
- Smoke test issue **UNI-162** "[StartupOS demo] connector smoke test — safe to delete" was created (and assigned to Kamal, due Sep 10) to observe the write path. Delete after the demo.

**Data pattern:**
- `BREX_SNAPSHOT` const — baked from Brex MCP (bank details stripped). `MODULE_SNAPSHOTS` const — build / marketing / web / customers / social / security / commerce / research; `renderModule(name)` builds each view (memory line, 4 tiles, signals, table(s), approvals, next integrations). Sources: Linear team UnitoneSentinel (16c53f3f-…), Vercel team UNITONE (team_rTpjbAHqJufb8oQPlY2yfUMQ; site prj_1vlPb44zU5MSYSeDO9HCunJXnu6k, threatmodel prj_YkQd4L6Lu8YQyZiHg1hkxYbd7GUE, shieldreply prj_9Al8iKyr97rvv3kIisPVOW6Te2fO).
- Sales `SEQUENCES` still last-known 2026-04-03 (Apollo MCP dial errors all session; Azure report path `~/unitone-gtm/reports/unitone_outbound_report.xlsx` no longer exists on the VM — layout changed; asked user for an `azure_inventory.txt` listing).
- Chart.js guarded with `typeof Chart === 'undefined'`.

**Known gaps / blockers:** Vercel Web Analytics not enabled (404); Vercel runtime-errors query times out; sandbox has no SSH key for azureuser@20.106.244.178 (reachable from the Mac's Cowork VM, key-denied) — ask the user to scp files into StartupAgents/ instead. Apollo MCP in this session exposes no campaigns_search/accounts_search tool. Marketing/Web/Customers/Social/Security remain snapshot-only.

**Demo caution:** Finance shows real UnitOne numbers (cash, contractor bills, founder wires). Offer a demo-mode mask before showing to outsiders.

Why: prevents re-searching for a server that doesn't exist and re-deriving the snapshot/live mechanics each session.
How to apply: "refresh" = open the artifact (live) or re-bake consts and republish; "start it" = open the artifact URL; "make X live" = add the tool to the manifest, observe one real request/response first, extend `EXEC` or a `loadXLive()`.
