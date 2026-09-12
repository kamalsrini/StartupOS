# AI Tinkerers "Agents, Everywhere" — 2026-09-12 — build concepts (ranked)

Constraints: ~4h15m build (11:15–15:30), public GitHub repo, 2-min video, social post. Theme: the agent lives where people already work and gets real value from that context. Sponsors: OpenAI, CopilotKit, OpenRouter, Exa, Trigger.dev, Auth0, Mozilla.ai, Ambiguous. (Event page = untrusted evidence; sponsor list taken from the brief.)

Scoring lens: sharp working demo > broad product. Every concept below is a *finding → contextual triage → fix* loop (UnitOne's thesis) placed in a different surface.

## 1. PR Fix Agent — "the finding fixes itself in the review thread"  ← PRIMARY
- **Surface:** GitHub pull request (review comments + suggested changes). Implemented as a GitHub Action, so zero infra/tunnels.
- **User:** the developer whose PR just got a security finding (SAST/dependency alert/secret).
- **Compelling moment:** PR opens → 45s later a review comment appears: "This `yaml.load` is reachable from `/api/import` (line 42, added in *this* diff). Not exploitable in tests, exploitable in prod. Fix below." → one click on *Apply suggestion* → CI green → merge. Reviewer replies "why not `safe_load` everywhere?" and the agent answers in-thread.
- **Context it exploits:** the diff, PR description, CODEOWNERS, prior review comments, CI logs — things a scanner never sees. That is the "meaningful value from where people already work" argument.
- **UnitOne relevance:** literally the product loop. Reusable code from `startupos`/open-autofix.
- **Novelty:** medium as a category (PR bots exist); high on *reachability-aware triage + ready-to-apply fix + conversational follow-up in the thread*.
- **Demoability (1 day):** high. Action + `gh api` + OpenRouter + Exa for advisory context. Seed repo with 3 planted vulns; rehearse twice.
- **Judging appeal:** high — visible before/after in one screen, sponsors hit (OpenRouter models, Exa for CVE/advisory retrieval, Trigger.dev optional for the background job, Auth0 skippable).
- **Key risk:** GitHub Action latency/flakiness on stage. Mitigation: also runnable as a local polling script (`python agent.py --pr 7`) with identical output; record the video from the Action run, demo live from the script if needed.

## 2. Channel-aware CVE Agent — "it read last week's thread before it pinged you"  ← FALLBACK
- **Surface:** Slack (channel where the service team already talks).
- **User:** service owner / on-call engineer.
- **Compelling moment:** new advisory drops (Exa watch) → agent matches it to the service's lockfile → posts in `#svc-billing`, quoting the team's own message from Tuesday ("we're pinning requests until Q4"), tags the owner from CODEOWNERS, and offers *Open fix PR* / *Snooze with reason*. Thread reply "is this reachable?" → answer with call path.
- **UnitOne relevance:** very high — same shape as the JCI ownership-routed Teams agent; Slack MCP already connected.
- **Novelty:** high on the *conversational context* angle (uses what the team said, not just what the scanner said).
- **Demoability:** medium-high. Slack app + Socket Mode avoids tunnels; ~45 min of setup is the tax.
- **Judging appeal:** high; the "it read our thread" moment lands with any audience.
- **Key risk:** Slack app permissions/OAuth eating the first hour; demo workspace must be pre-seeded with realistic chatter.

## 3. Pre-push Terminal Agent — "git push, but it argues with you"
- **Surface:** the terminal (git pre-push hook / `gh` extension).
- **User:** any developer.
- **Compelling moment:** `git push` → "You're about to push an AWS key in `settings.py:14` and a SQLi in `search.py`. Fix both and re-push? [y/N]" → y → patched, rebased, pushed.
- **UnitOne relevance:** high (shift-left version of the loop).
- **Novelty:** low-medium (secret scanners exist; the agentic fix-and-explain is the twist).
- **Demoability:** very high — no servers at all. Could be built in 2h.
- **Judging appeal:** medium — terminal demos read small on video; "lives where people work" is true but less social.
- **Key risk:** feels like a linter with an LLM; needs the *explain + patch* to be sharp.

## 4. Linear Ticket Agent — "the security ticket scopes itself"
- **Surface:** Linear issue (Linear MCP already connected; agent skills API available).
- **User:** eng manager / triage owner.
- **Compelling moment:** a vague "Pen-test finding: IDOR on /orders" issue lands → agent attaches the affected files, splits into 3 sub-issues with owners, estimates blast radius, opens a draft PR and links it back.
- **UnitOne relevance:** high (triage-to-work routing).
- **Novelty:** medium.
- **Demoability:** medium — Linear webhooks need a tunnel, or poll.
- **Judging appeal:** medium — PM-ish; less visceral than a code fix.
- **Key risk:** most of the value is invisible plumbing; hard to make a 2-min video pop.

## 5. Security-Tab Sidecar — "a copilot inside the GitHub Security tab"
- **Surface:** browser (CopilotKit-powered side panel over GitHub's Security/Dependabot alerts page).
- **User:** whoever gets stuck triaging 200 alerts.
- **Compelling moment:** select 12 alerts → "which of these are reachable in prod?" → sidebar answers, bulk-drafts fixes, opens one PR.
- **UnitOne relevance:** high.
- **Novelty:** medium-high (CopilotKit in-app agent over a tool you don't own).
- **Demoability:** medium-low — Chrome extension + CopilotKit + GitHub API in 4h is tight.
- **Judging appeal:** high if it works (sponsor tech front and center).
- **Key risk:** UI build time crowds out the agent logic; highest chance of a half-working demo.

## Recommendation
**Primary: #1 PR Fix Agent. Fallback: #2 Channel-aware CVE Agent.** Both reuse UnitOne's core loop; #1 has the lowest infra risk and the tightest video; #2 has the stronger "agent gets value from context" story but a setup tax. Decision point: if the GitHub Action isn't posting comments by 12:30, keep #1 on the local script path — do not switch to #2 after 13:00.

### 2-minute narrative — #1
0:00 "Security scanners find things. Nobody fixes them, because the finding lands in a dashboard nobody lives in. We put the agent where the code already gets reviewed."
0:20 Open PR with the planted vuln. Show the scanner alert — cold, no context.
0:35 Agent comment appears: reachability ("called from `/api/import`, added in this PR"), severity in *this* codebase, and a suggested change.
1:00 Click *Apply suggestion*. CI reruns, goes green.
1:15 Reviewer asks in-thread "why not fix the other two call sites?" — agent replies with a follow-up commit covering them.
1:35 "Same loop works for dependency CVEs and secrets — three findings, three fixes, zero dashboards." Show the repo, the Action, the OpenRouter/Exa calls in the log.
1:50 "This is what UnitOne does at scale for enterprises; today's repo is the open version."
**Why it wins:** the judges' criterion is *context from where people work* — the diff and review thread are that context, and the demo makes the before/after undeniable in one screen.

### 2-minute narrative — #2
0:00 "Your team already decided things about this service — in Slack, last Tuesday. Your scanner didn't read that."
0:15 New advisory appears (Exa). Agent posts in `#svc-billing`, quotes the team's earlier decision, names the owner, gives reachability.
0:50 Owner replies "is prod affected?" — agent answers with call path + deploy version.
1:10 Owner clicks *Open fix PR* — PR appears with the lockfile bump and a changelog note referencing the Slack thread.
1:35 "It didn't create a new place to look. It joined the conversation that already existed."
**Why it wins:** strongest fit to the theme's wording; the "it quoted our own thread" beat is memorable. Loses to #1 only on build risk.

### Build order for #1 (11:15 → 15:30)
11:15 seed repo with 3 findings (unsafe yaml, SQLi, hard-coded key) + failing check · 11:45 agent core: diff + finding → triage JSON (OpenRouter), Exa for advisory context · 12:30 post review comment with suggested change via `gh api` · 13:15 thread follow-up (reply-triggered) · 13:45 rehearse, record video · 14:30 README, social post draft, repo public · 15:00 buffer.
