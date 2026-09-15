# StartupOS — Project Reference

**Living document.** Update this with every material decision. Read at the start of every session.

**Last updated:** 2026-05-27 — Phase 1 path locked, memory-centric architecture established, UnitOne GTM confirmed as Sales substrate.

---

## Core thesis (one paragraph)

StartupOS is the AI-native operating system founders use to **build, grow, and manage** their startup — not generate it. It is a **persistent memory layer + module surface**. Mission, ICP, brand voice, pricing, team, decisions, customer feedback, and financial state all live in shared memory. Every module (Sales, Finance, Marketing, Build, Commerce) **retrieves from that memory** to do its work without starting cold. Pre-populated, approve-before-execute, MCP-native. Anti-Polsia (phantom completions), anti-Lev (stops at strategy), anti-Gumloop (blank canvas).

---

## TL;DR

The "AI-native founder OS" category is being claimed right now. The market leader by mindshare (Polsia) has destroyed user trust — 2.1/5 on Trustpilot, the top Hacker News thread on it is titled "fake ARR, dead users, god-mode over your company." The well-funded ideation play (Lev, ex-PSL) stops at strategy and never executes. The horizontal canvases (Gumloop, Lindy) require users to assemble their own workflows from a blank shell.

**The white space is a founder OS that opens with work already done** — opinionated, pre-embedded workflows in every module (especially outbound), with human-in-the-loop approval as the trust contract.

Lead with **Sales/Outbound** as the wedge. It's the most painful, most templatable, least owned by any incumbent OS, and you already have nine production GTM skills installed that drop directly into the module. Use Finance, Marketing, Build, and Commerce as breadth proof; depth in Sales sells the product.

Price between Stripe Atlas ($500 one-time) and Glean ($60k floor) — namely, **$99/mo Founder, $299/mo Team, $999/mo Growth**. Avoid credit-based billing (the #1 Gumloop complaint).

---

## 1. The competitive landscape, compressed

### Direct competitors (founder OS positioning)

| Product | Positioning | Pricing | Critical gap |
|---|---|---|---|
| **Polsia** | "AI that runs your company while you sleep" | $49/mo + 20% rev share | Phantom completions, no demand validation, hostile reputation (2.1 Trustpilot, HN backlash) |
| **Lev** (PSL) | "AI co-founder" | Free tier, paid TBD | Stops at strategy; no execution layer; fake testimonial headshots |
| **Cap (cap.vc)** | "Venture OS" — VC-side | Gated early access | Built for VCs, not founders. "StartupOS" surface is "coming soon" |

### Horizontal canvases (DIY substrate)

| Product | What it is | Why it's not a founder OS |
|---|---|---|
| **Gumloop** | Node-graph automation, YC W24, $50M Series B | Blank canvas. Credits-based, "expensive" cited 42× in G2 reviews |
| **Lindy / Relevance** | "AI employee" recipes, 2,300+ integrations | Recipes ≠ shaped product. Sales motion, not a founder UI |

### Vertical incumbents bolting on AI

| Product | What they own | Why they don't threaten the OS play |
|---|---|---|
| **Brex** | "Intelligent finance" with Audit/Review/Procurement agents | Finance only. Brex is what plugs into the Finance module, not a competitor |
| **Ramp** | Procurement agents, 16% savings | Spend management only |
| **Mercury** | Bank + sweep automation, Cmd+K | Banking only |
| **Notion AI** | $20/seat Business, Custom Agents (Notion 3.3) | Closest to "ambient OS" but no opinionated founder workflows. A docs surface, not an execution surface |
| **Glean** | Enterprise search + Work AI | $60k floor ACV — irrelevant to early-stage startups |
| **Pulley / Carta Launch / Stripe Atlas** | Cap table, equity, formation | Each owns a single workflow. Adjacent, not competitive |

### Where the category is converging

Three IA patterns are merging in 2026:
- **Sidebar modules** (Mercury, Ramp, Notion, Cap)
- **Command palette / Companion overlay** (Cap ⌘+., Mercury Cmd+K, Brex Assistant)
- **Agent roster as second nav** (Polsia, Brex)

The de facto pattern is becoming: **sidebar modules + command palette + agent-roster-as-secondary-nav.** Build to this.

---

## 1a. The memory-centric architecture (the unlock)

> **"Use our OS as the place to keep all memory about the startup and then retrieve it to use within different parts like finance and sales etc."** — Dilbert, 2026-05-27

This is the central architectural decision. StartupOS is **not** a collection of dashboards. It is a **memory store** with module-shaped retrieval.

**Memory schema (v0):**
- **Identity** — name, mission, vision, values, one-liner, elevator pitch
- **ICP** — segments, firmographics, technographics, intent signals, exclusions
- **Brand voice** — banned terms, preferred terms, tone examples, do/don't drafts
- **Pricing** — tiers, plans, exceptions, discount policy
- **Team** — roster, AORs (areas of responsibility), reporting lines
- **Decisions** — log of who decided what, when, why
- **Customer state** — accounts, contacts, engagement history, NPS, support themes
- **Financial state** — MRR/ARR snapshot, runway, burn, current month plan vs. actuals
- **Build state** — roadmap themes, in-flight initiatives, recent shipped, current incidents

**Retrieval pattern:** Every module surface starts by calling a `getMemory(slice)` that returns the relevant slice. Sales module pulls Identity + ICP + Brand voice + Customer state. Finance pulls Identity + Pricing + Financial state. Marketing pulls Identity + Brand voice + Customer state. This is what makes the OS open with work already done — the module already knows your company.

**Write-back pattern:** Module actions (closed deal, shipped feature, sent campaign) write events back to memory so the next retrieval sees fresh state. Workflows can subscribe to memory changes (e.g., "new $X MRR → post #wins").

**v0 storage:** localStorage in the artifact (for the prototype). v1 storage: hosted Postgres + per-tenant encryption. v2: vectorized for semantic retrieval, exposed via an MCP server so external agents can query the StartupOS memory.

---

## 1b. UnitOne GTM is the Sales substrate (don't rebuild it)

The user already runs a production outbound stack — **UnitOne GTM**, on an Azure VM at `azureuser@20.106.244.178`. 7 cron jobs, 9 Python scripts, 3 live Apollo sequences (841 contacts across VP Eng, CTO v2, CRA OT/BAS), PostHog cookieless analytics, Slack alert taxonomy (🔴 reply / 🟠 click / 🟡 multi-open / ⚪ bounce / 📊 health), Friday attribution merge.

**The StartupOS Sales module wraps this stack rather than rebuilding it.** The artifact:
- Reads live sequence stats from Apollo via MCP
- Reads engagement signals from PostHog via MCP
- Triggers the 9 `gtm-engine` skills as approve-before-execute actions
- Does NOT duplicate `auto_enroll`, `engagement_watcher`, `daily_report`, `attribution_merge` — those keep running on cron

This is the existence proof for the StartupOS thesis: a real, running execution stack that's only missing the unified founder-grade UI. StartupOS is that UI, generalized.

---

## 2. Positioning thesis

> **StartupOS — the founder operating system that opens with work already done.**
>
> Every other tool asks you to build your workflow. We ship the workflow. You approve, the agent executes. Sales pipeline populated on day one with enriched leads. Finance forecast live the moment Stripe connects. Marketing brand voice already enforced. Build module already syncing your roadmap from Linear.
>
> Price: $99 Founder, $299 Team, $999 Growth. No credits. No revenue share. No surprises.

The four positioning pillars:

1. **Pre-populated, not blank.** First login, every module has real, ranked, actionable work in it — not an empty state with a "+ New" button. Outbound queue with 50 enriched ICP-fit leads. Brand voice ingested from your existing site. Finance forecast running against your Stripe.
2. **Approve before execute.** Anti-Polsia trust posture. Every outbound email is shown before send. Every finance categorization is shown before commit. Every code change is shown before merge. Surveillance-free engineer summaries (no commit-count shaming).
3. **Wedge → expand.** Lead with Sales/Outbound (your nine gtm-engine skills are the differentiator already in hand). Use Finance + Marketing + Build + Commerce as proof of breadth. Customers pay for Sales depth and stay for the OS.
4. **MCP-native, not screen-scraped.** Every module is wired to first-party MCPs (Stripe, GitHub, Linear, Notion, Apollo, HubSpot, Ahrefs, Klaviyo, Shopify, Chargebee, PagerDuty, Sentry). Pipedream/Composio for the long tail. No fragile scrapers, no dead integrations.

---

## 3. Proposed information architecture

```
┌─ Sidebar ─────────────────┬─ Cockpit (default home) ─────────────────────────────┐
│                           │                                                       │
│  ◉ Cockpit                │  Mission • Mood • Today's priorities                  │
│  ─ Sales                  │  Weekly health score (auto-rolled up)                 │
│  ─ Marketing              │  Pre-staged work waiting on you (across all modules)  │
│  ─ Build                  │                                                       │
│  ─ Finance                ├──────────────────────────────────────────────────────┤
│  ─ Customers              │  Module quick-glance cards (revenue, pipeline,        │
│  ─ Research               │   incidents, hiring) — click to drill into module     │
│  ─ Social                 │                                                       │
│  ─ Web                    ├──────────────────────────────────────────────────────┤
│  ─ IT & Security          │  Agent activity feed (what ran overnight, what's      │
│                           │   waiting for your approval, what needs your call)    │
│  ⌘K Command palette       │                                                       │
│  ⓘ Agents                 │                                                       │
│                           │                                                       │
└───────────────────────────┴───────────────────────────────────────────────────────┘
```

### Module-by-module v1 scope

| Module | Status | Backed by | Pre-embedded workflows on day one |
|---|---|---|---|
| **Cockpit** | Full build | Internal + Notion MCP | Weekly health score, agent activity feed, mood, priorities |
| **Sales / Outbound** | **Full build (wedge)** | Apollo, HubSpot, Salesforce, Crustdata, Gmail MCPs + 9 gtm-engine skills | Daily pulse, sequence builder, hot-lead response, warm-intro graph, competitor teardown, ICP scoring, creative angles, state sync, weekly review |
| **Finance** | Full build | Stripe, QBO, Chargebee MCPs | MRR/ARR dashboard, runway forecast, anomaly alerts, board-pack draft |
| **Marketing / Web / Social** | Full build | Ahrefs, Semrush, GA4, Klaviyo, HubSpot, Canva, Figma MCPs | Content calendar, brand drift detector, SEO radar, reactive content engine |
| **Build / Coding** | Full build | GitHub, Linear, PagerDuty, Sentry MCPs | Roadmap auto-sync, PR nudges, incident postmortem loop, release notes |
| **Commerce** | Full build | Stripe, Shopify, Chargebee MCPs (Square, Recurly via Pipedream) | Unified subscription ledger, dunning, dispute evidence pull, top-customer alerts |
| **Customers / Research / Social / Web / IT / Security** | Stubbed in v1 | — | Module shell + "coming Q3" copy; routes exist so URLs are stable |

### Mapping your existing gtm-engine skills into the Sales UI

| Skill | Surface | Trigger type |
|---|---|---|
| `gtm-daily-pulse` | Sales home, top panel | Scheduled 7am local + always-visible |
| `gtm-hot-lead-response` | Inline button on every inbound reply card | User click |
| `gtm-sequence-builder` | "+ New Sequence" wizard | User click |
| `gtm-enrich-and-score` | Bulk action + auto on CSV import | User click + background job |
| `gtm-warm-intros` | Account detail sidebar widget | Always-visible |
| `gtm-competitor-teardown` | Battlecards tab + reactive on reply mentions | User click + reactive |
| `gtm-creative-angles` | Account detail "Stuck? Try new angle" affordance | Conditional (shown after 3+ no-reply touches) |
| `gtm-state-sync` | Settings → Data health | Nightly scheduled + manual button |
| `gtm-weekly-review` | Dashboard tab + Friday 4pm email | Scheduled weekly |

This is the differentiator. Nine production workflows already shaped to your voice — no other founder OS has anything close.

---

## 4. Pricing

| Tier | Price | Who it's for | Includes |
|---|---|---|---|
| **Founder** | $99/mo | Pre-seed, solo founder | All modules read-only or single-user. Daily limits on AI calls. 1 seat. |
| **Team** | $299/mo | Seed, 2–10 people | Multi-seat (up to 10), full agent execution, all MCP integrations, 3 customer success calls |
| **Growth** | $999/mo | Series A, 11–50 | Unlimited seats, dedicated workspace, SSO, audit log, priority MCP support |

**Why this works:**
- Above Polsia's $49 (which is correlated with the trust problem — too cheap to be real)
- Below Glean's $60k floor (which excludes the entire seed market)
- Flat, not credit-based (avoids Gumloop's #1 complaint)
- No revenue share (Polsia's 20% is universally hated)

---

## 5. Phased build path (recommendation)

**Phase 0 — Validation (this week, 1–2 sessions):**
- Build a clickable HTML prototype with all 11 sidebar modules. Cockpit + Sales fully fleshed (real interactions, fake data). Finance/Marketing/Build/Commerce showing the proposed UI shape. Others as labeled stubs.
- Publish to a Vercel/Cloudflare Pages URL behind a teaser landing page with a waitlist form.
- Goal: 50 founders on the waitlist before any backend code is written.

**Phase 1 — Sales wedge MVP (4–6 weeks):**
- Real backend: Apollo + HubSpot + Gmail MCP integrations live.
- All nine gtm-engine skills wired into the Sales module UI.
- Cockpit shows real activity feed from Sales actions.
- Stripe billing live at $99/mo single tier (Founder only).
- Goal: first 25 paying founders.

**Phase 2 — Breadth (Q3 2026):**
- Finance module live (Stripe + QBO MCP).
- Build module live (GitHub + Linear MCP).
- Marketing module live (Ahrefs + Klaviyo MCP).
- Commerce module live (Stripe + Shopify MCP).
- Team tier ($299) opens.
- Goal: 200 paying customers, $50k MRR.

**Phase 3 — Depth (Q4 2026):**
- Customers/Research/Social/Web/IT/Security modules.
- SSO, audit log, SOC2 prep.
- Growth tier ($999) opens.
- Goal: First $500k ARR mark.

---

## 6. Decision log

**2026-05-27 — Path C selected.** User chose to skip the clickable prototype and jump to Phase 1: a live Cowork artifact for the Sales module wired to Apollo + PostHog, with the StartupOS memory layer demonstrated as the cross-module foundation. Memory-centric architecture committed as the spine of the product.

**Active deliverable:** Cowork artifact at `/Users/kamal/StartupAgents/startupos_sales_module.html`, registered to refresh on reopen, calling Apollo MCP and PostHog MCP when authorized.

**2026-05-27 — Phase 2 module 1 = Finance.** User chose Finance as the second module to build out, with bill-pay (AP) + customer invoices (AR) + incoming-funds rollup as the v1 scope. Stack: Brex MCP (cards/expenses/limits — bill pay tools NOT exposed in MCP), everything else from memory/spreadsheets initially. **Trust contract: StartupOS never moves money** — bills surface as drafts; "Approve" deep-links to Brex's own bill pay UI where the user clicks Pay. New memory slices: `bills`, `invoices`, `payments`, `cashOnHand`, `burnRate` ([[finance-schema]] in memory).

**2026-09-03 — Demo build: breadth across 8 modules.** For a prospective-founder demo, extended the single-file artifact from 3 populated modules to 8 (Sales, Finance, Build, Marketing, Web, Customers, Social, IT & Security; Commerce + Research remain shaped stubs with a connect action). Pattern: every module = Startup Memory slice → baked MCP snapshot (Brex, Linear, Vercel) with "○ snapshot · age" badge → signal feed → approve-before-execute drafts. Brex snapshot re-baked (cash $517.26, 9 bills, 22 wires). Apollo MCP was down at snapshot time and the sandbox has no SSH key for the Azure VM, so Sales/Social carry last-known Apr 3 numbers. Published as a Claude artifact (stable URL) for presenting from any screen. **Clarification:** there is no local server for StartupOS — the "Brex snapshot loop" was a scheduled task (`refresh-brex-snapshot`, no longer registered) that re-baked live data into the HTML; the Azure VM is the GTM backend, not an app server.

**2026-09-04 — Independent runtime decided.** StartupOS moves off the Cowork artifact runtime to a self-hosted service: company brain (Postgres + pgvector + `brain/` Markdown repo), deterministic ingest and signal engine, one Claude Agent SDK daemon, approval gate, FastAPI + Next.js. Token strategy: no model in ingest/polling/rendering; nightly compiled context pack with prompt caching; three compute tiers; per-tenant budgets with degradation instead of overage billing; BYO-key option. Target token COGS $8–20/month per Founder seat. Full detail in `StartupOS_Architecture_Brief.md` (companion doc, canonical alongside this one).

**2026-09-04 — Sprint 1 shipped (independent runtime v0.1).** Codebase at `StartupAgents/startupos/` (local git). Brain (10 seed docs, FTS retrieval, context pack), deterministic ingest (Linear/Slack/Brex/Vercel) + 12 signal rules + digest, agent daemon (tiered LLM gateway with prompt caching, budgets, run ledger; approvals; Linear/Slack executors; 7 skills; scheduler; Slack gateway), FastAPI + Next.js onboarding/cockpit. All gates green (151 unit / 8 functional / 4 regression). Process: `startupos-sdlc` skill — read both briefs, task list, parallel agents, lint/unit/functional/regression gates, PE review. Next: keys in `.env`, first live run, Azure compose deploy, Apollo ingest (Week 3).

**2026-09-04 — Sprint 2 shipped: tenant isolation, login, Chief of Staff.** Row Level Security on every tenant table with an RLS-bound DB role; Google OIDC + signed-cookie sessions + hashed API tokens + one-time bootstrap owner; one company-wide role for now (roles inside a company deliberately not separated yet); tenant always derived from the signed-in user. New hub agent `cockpit.chief_of_staff`: sits above the spokes (Sales, Marketing, Customers, Finance, Build, Web, Social, Security), reads the whole brain plus Tier-0 lookahead facts, and raises dated "will happen" vs pattern "might happen" risks per spoke before they land — daily 06:30, on demand via Slack ("what should I worry about") or `/asks`. First real run cost $0.05 and surfaced five risks with issue ids. Gates green (200 unit / 30 functional / 7 regression). Deploy needs the new env vars (session secret, bootstrap token, app-role password) before the API will start.

**Decisions pending:**
- Whether to wire Stripe MCP next (would auto-populate AR + payments from real customer charges).
- Mercury MCP for cash position visibility (read-only, low-cost addition).
- Workspace storage migration — when to move from localStorage to hosted Postgres. Likely after first 5 design partners.

---

## Sources

**Competitor research:**
- [Polsia homepage](https://polsia.com)
- [Polsia HN thread (id 48252194)](https://news.ycombinator.com/item?id=48252194)
- [Polsia Trustpilot](https://www.trustpilot.com/review/polsia.com)
- [Lev (getlev.co)](https://getlev.co/)
- [GeekWire — T.A. McCann / Lev](https://www.geekwire.com/2026/psls-t-a-mccann-is-running-a-startup-again-as-the-ceo-of-lev-an-ai-co-founder-for-startups/)
- [Cap.vc — Venture OS](https://cap.vc/)
- [Gumloop pricing](https://www.gumloop.com/pricing)
- [Gumloop Series B coverage](https://siliconangle.com/2026/03/13/gumloop-reels-50m-ai-automation-platform/)
- [Brex — Intelligent Finance](https://www.brex.com/platform/intelligent-finance)
- [Ramp Intelligence](https://ramp.com/intelligence)
- [Mercury pricing](https://mercury.com/pricing)
- [Notion pricing](https://www.notion.com/pricing)
- [Glean pricing breakdown](https://www.gosearch.ai/blog/glean-pricing-explained/)
- [Pulley pricing](https://pulley.com/pricing)
- [Carta Launch](https://carta.com/equity-management/launch/)
- [Stripe Atlas](https://stripe.com/atlas)

**MCP / connector research:**
- [Stripe MCP docs](https://docs.stripe.com/mcp)
- [Intuit QuickBooks Online MCP](https://github.com/intuit/quickbooks-online-mcp-server)
- [Notion MCP](https://developers.notion.com/guides/mcp/overview)
- [GitHub official MCP server](https://github.com/github/github-mcp-server)
- [Linear MCP changelog](https://linear.app/changelog/2026-02-05-linear-mcp-for-product-management)
- [PagerDuty MCP server](https://github.com/PagerDuty/pagerduty-mcp-server)
- [Chargebee MCP](https://www.chargebee.com/docs/billing/2.0/ai-in-chargebee/chargebee-mcp)
- [Shopify MCP guide 2026](https://wearepresta.com/shopify-mcp-server-the-standardized-interface-for-agentic-commerce-2026/)
- [Apollo MCP / sales stack](https://crustdata.com/blog/best-mcp-servers-for-sales-teams-in-2026)
- [Best MCP servers for marketers 2026](https://segmentstream.com/blog/articles/best-mcp-servers-for-marketers)
- [MCP server ecosystem tracker 2026](https://www.digitalapplied.com/blog/mcp-server-ecosystem-tracker-50-servers-cataloged-2026)

**2026-09-12 — Sprint 3a shipped: multi-tenant runtime (the SaaS test).** A second company can now sign up through the API, paste its own Linear key, and receive a real morning pulse with no operator involvement — the acceptance the 2026-09-12 audit said was failing. Per-tenant credentials are envelope-encrypted (`tenant_secrets`, AES-GCM, `STARTUPOS_MASTER_KEY`); the daemon schedules every job per tenant and discovers new tenants every 5 minutes; ingest iterates tenants × their connections; onboarding "compile" enqueues an ordered chain (backfill → signals → context pack → Chief of Staff → pulse) serviced by the daemon with retries, and the wizard shows the chain's progress. PE review found and fixed two pre-existing cross-tenant defects that Sprint 3a would have made exploitable: any tenant could point a connection at the operator's `env:` keys, and executors ran every tenant's approved Linear/Slack actions with the operator's credentials. Gates green (240 unit / 54 functional / 7 regression). One new required env var: `STARTUPOS_MASTER_KEY`. Remaining for the SaaS story: Slack is still one workspace per install (Sprint 3b), Sales wedge (3c).

**2026-09-13 — Sprint 3b shipped: StartupOS lives in Slack.** The founder no longer has to open the web app. Each tenant installs StartupOS into its own Slack workspace over OAuth (one distributed Slack app, per-tenant bot tokens encrypted in `tenant_secrets` — never env vars); the morning pulse, the evening digest and high-severity signals (plus every Chief of Staff risk) post to the channel that tenant chose, deduped by a `deliveries` ledger; and approvals arrive as Block Kit messages with Approve/Decline buttons that execute through the existing approval gate with that tenant's own credentials and then edit the message in place, so a button cannot be pressed twice. Inbound Slack (events, DMs, button presses) is signature-verified with a 300-second replay window, resolves its tenant from `team_id` alone, acknowledges inside Slack's 3-second budget by queueing to `tenant_jobs`, and is answered by the daemon. Uninstall revokes the install, deletes the token and disables the connection. Gates: 365 unit / 118 functional / 10 regression, including an offline end-to-end regression that walks install → signal → post → button → execute → DM → uninstall for two tenants in two workspaces and proves neither can see or act on the other. Three new install-level env vars (`STARTUPOS_SLACK_CLIENT_ID/SECRET/SIGNING_SECRET`); with them unset — today's production state — every Slack route answers 503 and nothing else changes. Setup runbook: `docs/SLACK-SETUP.md`. **Blocker for going live: Slack must reach the API over HTTPS, and the VM today serves plain HTTP on a port closed in the NSG.** Remaining: Sales wedge (Sprint 3c).

**2026-09-15 — Sprint 3d shipped: one URL to share.** StartupOS becomes something a founder can hand to another person. The Next.js app is deployed for the first time (it was never in the compose file), Caddy sits in front with an automatically issued certificate, and the whole product lives on one hostname — `https://<domain>/` for the app, `https://<domain>/api/…` for the API — so sessions are same-site and there is one certificate, no CORS. Sign-in is Google end to end, and a verified Google identity nobody has seen before is offered a company of its own rather than the old "ask your owner to invite you" dead end; joining an existing company stays invite-only. The VM's public surface becomes 22, 80 and 443 — the API and Postgres move to loopback. Going public changed the threat model, and the PE review fixed five real defects before launch: an unthrottled second door for guessing the bootstrap token, a rate limiter that behind a proxy throttled everyone into one global bucket, one Google account able to mint unlimited tenants (and therefore unlimited model spend), an open redirect on the sign-in page, and a flaky auth test. Self-serve tenants now get a deliberately small model budget (`STARTUPOS_SIGNUP_TIER2_TOKENS`, default 100k versus 1.5M for operator-created), sign-up has an operator off switch, and the API docs no longer serve on a public deployment. Gates: 421 unit / 132 functional / 10 regression, web build/typecheck/lint clean. Runbooks: `docs/HOSTING.md`, `docs/GOOGLE-SIGNIN.md`. PE verdict: safe to expose publicly once the domain, the Google OAuth client and the production `.env` are in place.
