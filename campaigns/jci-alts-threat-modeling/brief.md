# Campaign — JCI Alternatives · Automated Threat Modeling → Agentic Remediation

**Status:** Draft for morning review. **Nothing sends until you approve.**
**Drafted:** 2026-06-03 overnight
**Owner:** Kamal · UnitOne
**Stage in StartupOS:** Sales module → Campaigns → `jci-alts-threat-modeling`

---

## TL;DR

A new outbound wave to the JCI competitor set — Honeywell, Siemens, Schneider, Carrier/Automated Logic, Trane, Distech, Delta Controls, KMC, Reliable Controls — with a different angle than the existing CRA OT/BAS sequence. This one targets **VP Product Security / Head of Product Security / VP Engineering**, not the CISO/Compliance buyer. The hook: **the `/spine` demo shows UnitOne finding live CVEs in their own products and their competitors' products — before the public NVD entry**. Proof-by-screenshot, not proof-by-claim.

## Why this campaign and why now

Memory snapshot of UnitOne's GTM state:
- Wave 1 — **CRA OT/BAS** sequence: 763 contacts at Emerson, Schneider, Honeywell, Siemens + 6 more. Compliance angle. Buyers: CISO + Compliance Lead. Launched 2026-04-03. ([[unitone-gtm-stack]])
- Gap: the *product security* persona at these same vendors has not been engaged. They have a different pain — vulnerabilities in their own shipping products — and a different budget line.
- Trigger: the `/spine` demo is now mature enough to show *real* fleet-profile-loaded CVEs in Honeywell IQ4x, Niagara, Metasys, BACnet stacks, and Lutron Quantum. This is a **demo-driven outbound** play.

## Positioning thesis

> **Most product security teams find their own vulns after CISA does.** UnitOne finds them in the design phase, before code lands, and runs an agentic loop that proposes the fix and ships a PR. We'll show you what we've already surfaced in your shipping products.

Three concrete claims (all visible in `/spine`):
1. **DESIGN tier finds threats in upstream protocol implementations.** Demo shows `BACnet Stack (C) + BACpypes (Python) Unified BACnet System` returning 4 crit / 4 high / 4 med threats with score 0 (untriaged).
2. **DEVELOP tier closes them with fix confidence ≥ 90%.** Demo shows real CVEs (Honeywell IQ4x BMS-CVE-2026-3611, Niagara NVD 2018-08-20, Metasys path traversal, BACnet protocol stack CVE 2019-05-30) with combined-signal fix confidence (scanner agreement × callgraph reach × skill match × differential verify).
3. **DEPLOY tier provides regulatory evidence.** Demo shows 47 vulns closed, exposure window collapsed from 23d → 6h, SOC2 CC6.6 + GDPR Art 32(1)(a) evidence pack signed, CRA Art 5 evidence ✓.

## ICP and Persona

**Primary persona — VP Product Security / Head of Product Security**
- Owns: CVEs in shipping products, coordinated disclosure response, the firmware/software security org
- Pain: PSIRT queue depth, time-to-fix on known CVEs, internal embarrassment when researchers find issues first
- Budget: ~$200k–$1.5M for tooling, no single decision-maker but they're the technical champion

**Secondary persona — VP Engineering / CTO of the BMS business unit**
- Owns: engineering velocity, ship dates, security as a feature
- Pain: security fixes derailing roadmap, dependency hell in firmware
- Budget: larger but slower

**Tertiary persona — Director of OT Security / Plant IT Lead at customers of those vendors** (deferred — this is more like the CRA OT/BAS wave)

## Target accounts (Wave 2.0 — 18 accounts)

See [`targets.md`](./targets.md) for the full table with ICP scoring rationale, target persona names where known, and per-account hook.

Tier 1 — Major vendors (8):
1. Honeywell Building Technologies (incl. Tridium/Niagara)
2. Siemens Smart Infrastructure
3. Schneider Electric Buildings (EcoStruxure)
4. Carrier Global / Automated Logic
5. Trane Technologies
6. ABB Smart Buildings
7. Mitsubishi Electric Building Automation
8. Daikin Applied

Tier 2 — Pure-play BAS (7):
9. Distech Controls (Acuity Brands)
10. Delta Controls (Delta Electronics Group)
11. KMC Controls
12. Reliable Controls
13. Cylon Auto-Matrix (Acuity Brands)
14. Crestron Electronics
15. Lutron Electronics

Tier 3 — Adjacent automation (3):
16. Belimo
17. Ingersoll Rand (industrial side)
18. Rockwell Automation (industrial overlap)

## Channel and sequence

4-touch email sequence over 12 calendar days + 2 LinkedIn touches. See [`sequence.md`](./sequence.md) for full copy.

| Touch | Day | Channel | Theme | Hook |
|---|---|---|---|---|
| T1 | Day 0 | Email | Cold open + demo link | "We found N CVEs in [their product]" |
| LI-1 | Day 2 | LinkedIn connect | No pitch, contextual | Reference one specific finding |
| T2 | Day 3 | Email reply-to-prior | New angle | "Your competitor [X] just patched a similar issue" |
| T3 | Day 7 | Email | Concrete proof | Specific CVSS calc walk-through from `/spine` |
| LI-2 | Day 9 | LinkedIn DM | Soft ask | "Worth a 15-min look?" |
| T4 | Day 12 | Email | Break-up | "Closing the loop — last note unless you'd like to reopen" |

Exit conditions: reply, click → demo, OOO, bounce, unsubscribe.

## Landing pages — per-account teardown

Each Tier 1 account gets a custom teardown page modeled on the existing 25 CTO Teardown pages but with **/spine evidence baked in**. The page shows:
- Their company name and one product (e.g. Honeywell IQ4x)
- The actual `/spine` fleet-profile view filtered to their products
- The exposure-window collapse (23d → 6h) animation
- CTA: "See your full fleet profile — 15 min, no commitment"

These get UTM-tagged links from emails so PostHog attribution works ([[unitone-gtm-stack]] — `content_distributor.py` handles UTM generation; `inject_posthog.py` for tracking).

## Approval gate (the trust contract)

**Per the StartupOS thesis: nothing sends without you.** Drafts land in the Approve Queue. You review each persona's email pack before the campaign goes live. You approve target list before `auto_enroll.py` adds to Apollo.

Recommended approval order tomorrow morning:
1. Validate target accounts (10 min)
2. Read T1 email, edit for voice — same edit applies to all variants (5 min)
3. Approve LI-1 connect note template (2 min)
4. Approve to enroll Tier 1 in Apollo (one button)
5. Tier 2 and Tier 3 deferred until Tier 1 sees 1+ reply

## Tooling and infra

- **Apollo sequence:** new sequence `JCI Alts — Threat Modeling v1` (do NOT use existing CRA OT/BAS — different persona, different messaging)
- **Enrollment:** runs via `auto_enroll.py` on Azure VM (existing cron at 7am ET M–F, cap 25/day)
- **LinkedIn followups:** queued by `linkedin_followup_queue.py` 9am ET daily
- **Engagement alerts:** `engagement_watcher.py` 3×/day fires 🔴 reply / 🟠 click / 🟡 multi-open / ⚪ bounce to Slack
- **Attribution:** Friday `attribution_merge.py` rolls up multi-channel ([[unitone-gtm-scripts]])

## Open questions (decide tomorrow)

1. **Honeywell** — they own Tridium/Niagara. Either skip them (conflict of interest — we'd be selling them tools that find their own bugs) or lean into it ("we're already finding issues in your Niagara stack, want a partnership conversation?"). My read: skip from Wave 2.0 outbound, hold for direct intro path.
2. **Cadence** — once Tier 1 launches, what's the next-day pulse trigger threshold? Suggest: any reply → manual handoff via `gtm-hot-lead-response`. Any click → 24h LinkedIn DM via the existing queue.
3. **Demo link sharing** — `/spine` is currently at `unitone-demo.eastus.cloudapp.azure.com`. Worth a memorable URL for the email CTA? Suggest: `unitone.ai/spine-demo` as a vanity redirect.

## Files in this folder

- `brief.md` — this file
- `targets.md` — 18 target accounts with ICP scoring rationale and hooks
- `sequence.md` — full email + LinkedIn copy for VP Product Security persona, with notes on VP Eng / CTO variants
- `landing-page-spec.md` — per-account teardown page concept and content blocks

See also: StartupOS artifact (Sales module → Campaigns) once staged.
