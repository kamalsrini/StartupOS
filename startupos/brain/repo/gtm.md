---
slice: gtm
updated: 2026-09-04
source: human
sequences: [{"id": "vp-eng", "name": "VP Eng", "contacts": 42}, {"id": "cto-v2", "name": "CTO v2", "contacts": 36}, {"id": "cra-ot-bas", "name": "CRA OT/BAS", "contacts": 763}]
campaigns: [{"id": "jci-alts-wave-2", "name": "JCI Alts wave 2.0", "status": "draft", "since": "2026-06-03", "targets": 18}]
---
# GTM state

## Apollo sequences (3)
| Sequence | Contacts | Notes |
|---|---|---|
| VP Eng | 42 | Security teardown as the asset |
| CTO v2 | 36 | Second-iteration CTO messaging |
| CRA OT/BAS | 763 | CRA compliance interactive demo page linked from every email |

Total reach: 841 contacts. Attribution: PostHog cookieless, Friday merge with Apollo.

## Campaigns
- **JCI Alts wave 2.0** — in draft since 2026-06-03. 18 scored targets, 4 touches written. Tier 1 (≥85): Siemens SI 92, Schneider Buildings 90, Carrier/Automated Logic 87, Trane 85, ABB Smart Buildings, Mitsubishi BA, Daikin Applied. Honeywell/Tridium excluded (HOLD). Persona: VP Product Security.

## Engagement pages
Security teardown (Mar 30), Guidewire teardown (Mar 31, unreferenced by any sequence), CRA compliance demo (Apr 3), ROI page.

## Rules
- Campaign in draft for more than 14 days → medium signal (`sales.stale_draft_campaign`).
- A reply in any sequence → high signal, draft a response in voice.
