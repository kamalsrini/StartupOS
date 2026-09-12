---
name: jci-alts-campaign
description: "JCI Alts — Automated Threat Modeling campaign drafted overnight 2026-06-03, awaiting Dilbert's morning approval. Wave 2.0 outbound to BAS competitor vendors, using /spine demo evidence as proof."
type: project
---

**Campaign drafted 2026-06-03 overnight, awaiting morning approval.**

**Files:** `StartupAgents/campaigns/jci-alts-threat-modeling/`
- `brief.md` — campaign overview, positioning, ICP, channel plan
- `targets.md` — 18 accounts with ICP scoring (7 Tier 1 + 7 Tier 2 + 3 Tier 3 + 1 hold)
- `sequence.md` — 4-touch email + 2-touch LinkedIn sequence, primary VP Product Security persona, variants for VP Eng/CTO and CISO
- `landing-page-spec.md` — per-account teardown page concept built on existing 25-page Vercel pattern

**Why:** Existing CRA OT/BAS sequence (Wave 1) targets CISO/Compliance with regulatory angle. This wave (2.0) targets a different persona — VP Product Security — at the same accounts plus expansion, with a different angle — find-CVEs-in-your-own-products before NVD. Hook is demo-driven: the `/spine` view literally shows live CVEs in Honeywell IQ4x, Niagara, Metasys, BACnet, Lutron Quantum.

**Special handling:** **Honeywell is on hold** for this wave (conflict-of-interest: they own Tridium/Niagara, and we'd be selling them a tool that finds bugs in their own products). Better path: warm intro via advisory network, not cold outbound. Tier 1 launch should be 7 accounts (ex-Honeywell), not 8.

**Demo URL referenced:** `https://unitone-demo.eastus.cloudapp.azure.com/spine`
- Lifecycle: Design (synthesis + agentic threat models) → Develop (active findings with fix confidence) → Deploy (posture-against-policy, attestation, regulatory evidence)
- CTO lens: PRs merged 47 · review latency 4 min · queue depth 12 · fix confidence 94%
- CISO lens: vulns closed 47 · exposure window 23d → 6h · CRA Art 5 evidence ✓
- BMS FLEET tab shows real CVEs filtered to: Honeywell IQ4x BMS-CVE-2026-3611 (CVSS 9.8 with asset weighting), Niagara NVD 2018-08-20 (EPSS 86th pct), Metasys path traversal NVD 2021-02-19, BACnet protocol stack NVD 2019-05-30 (EPSS 95th pct), BACnet Carel pCOWeb NVD 2022-08-31 (EPSS 99th pct), BACnet Lutron Quantum NVD 2018-04-23

**StartupOS surface:** The campaign is staged in the Sales module of the StartupOS artifact (`startupos-sales`, v7). User sees a Draft Campaigns card with approve / edit / open-brief / open-demo buttons. "Approve Tier 1" routes a chat prompt that creates the Apollo sequence and enrolls but pauses before sending — user does the final go in Apollo themselves per trust contract.

**Hand-off to existing automation when approved:**
- New Apollo sequence: `JCI Alts — Threat Modeling v1`
- 4 steps · Day 0 / 3 / 7 / 12 · pause-on-reply ON · pause-on-bounce ON · skip-weekends ON
- Enrolment via `auto_enroll.py` (existing cron 7am ET M-F, 25/day cap)
- Engagement alerts via `engagement_watcher.py` (existing 3×/day Slack alerts)
- LinkedIn follow-ups via `linkedin_followup_queue.py` (existing 9am ET cron)
- Attribution via Friday `attribution_merge.py`

Related: [[unitone-gtm-stack]], [[unitone-gtm-scripts]], [[startupos-vision]].
