# Landing pages — per-account teardown spec

Each Tier 1 + Tier 2 account gets a custom URL: `unitone.ai/spine/{slug}` where slug = company kebab-case (e.g. `siemens-smart-infra`, `schneider-buildings`, `carrier-automated-logic`).

These build on the existing 25 CTO Teardown + 25 ROI page pattern but with `/spine` demo evidence baked in instead of generic case studies.

## Page structure (single scroll, ≤ 4 sections)

### Section 1 — Above the fold (no scroll required)

- **Hero text** (≤ 12 words):
  - Default: "Threats in `{PRODUCT_REFERENCE}` — what we've already found."
  - Variant: "`{COMPANY_NAME}` · `/spine` fleet view"
- **Subhead** (1 line):
  - "Automated threat modeling. Agentic remediation. Closed loop in 4 minutes."
- **Hero visual:** screenshot of `/spine` CTO view with the BMS FLEET tab active, filtered to their products (Honeywell IQ4x, Niagara, Metasys, BACnet, Lutron Quantum — whichever applies).
  - Annotation overlay: arrow pointing at the line "Combined signal: scanner agreement × callgraph reach × skill match × differential verify" with caption "this is the fix-confidence math".
- **CTA:** primary button "See the live demo with your fleet pre-loaded → 15 min, no commitment"

### Section 2 — The three claims (one row each)

Three rows. Each row: short heading + 1-sentence description + screenshot.

1. **Design — synthesis pass on protocol stacks**
   - Screenshot: `/spine` Design view showing "BACnet Stack (C) + BACpypes (Python) Unified BACnet System · 4 crit / 4 high / 4 med / 0 low · score 0 untriaged"
   - Caption: "Threat models generated before code lands. Source: live `/spine` demo, 2026-06-03."

2. **Develop — fix confidence ≥ 90% from combined signal**
   - Screenshot: `/spine` Develop view showing "Fix Confidence 94% · Combined signal: scanner agreement × callgraph reach × skill match × differential verify"
   - Caption: "Not 'AI suggested a patch'. Mathematically combined evidence the patch will hold."

3. **Deploy — regulatory evidence, posture, attestation**
   - Screenshot: `/spine` Deploy view showing "Posture-against-policy 94% · Attestation coverage 100% · Control gaps 0 · SOC2 CC6.6 + GDPR Art 32(1)(a) evidence pack signed"
   - Caption: "Auditors get the evidence pack. Engineers get the PRs."

### Section 3 — Their specific findings (3 cards, account-specific)

For Siemens, this section shows:
- Card 1: "Desigo CC v9 · BACpypes integration · 4 crit / 4 high / 4 med synthesis findings"
- Card 2: "SLX controller firmware · upstream BACnet stack CVE 2019-05-30 · EPSS 95th percentile"
- Card 3: "Building X platform · 12 threats surfaced in synthesis pass · 0 triaged"

Each card has the CVSS scoring breakdown visible (REACHABLE / CVSS BASE / EPSS / KEV — matches the `/spine` Internals tab layout).

For Schneider, swap to EcoStruxure-specific findings. For Carrier, WebCTRL + Automated Logic findings. Etc.

### Section 4 — CTA + footer

- **Secondary headline:** "15 minutes. Your products. Real findings."
- **Form:** name, work email, calendar suggestion (3 time slots from sender's Calendly)
- **Trust footer:** "We do not share or publish findings without your PSIRT's coordinated disclosure timeline."

## Visual style

Match the `/spine` aesthetic exactly:
- Font: system-ui (matches Cowork artifact)
- Color: dark mode default with light mode toggle, mirroring the demo's sun icon
- Severity colors: red for crit, orange-red for high, amber for med, gray for low (same as demo)
- Status pills: green ACTIVE, blue SENT, gray DRAFT, red OVERDUE — matches both demo and StartupOS Finance module styling

This keeps the demo-to-landing-page-to-product experience visually continuous.

## Hosting

Deploy as static files to existing Vercel project (where `unitone.ai/blog` and demo landing pages already live). Path pattern: `unitone.ai/spine/{slug}`.

UTM-tagged links from email per existing `content_distributor.py` UTM taxonomy:
- `?utm_source=outbound&utm_medium=email&utm_campaign=jci-alts-tm-v1&utm_content=t{N}-{account_slug}`

PostHog tracking via `inject_posthog.py` — already cookieless, already on Vercel deploys.

## Build effort

- **15 accounts × 1 page each** = 15 landing pages
- Template the page (reuse Hero + 3 claims + footer)
- Per-account data: 3 findings cards. Could be a JSON file per account, rendered from template.
- ~4 hours of work for the template + 15 min per account to fill the data — about 8 hours total for Tier 1 + Tier 2

Suggest: build template tomorrow after morning approval, populate Tier 1 (7 accounts) first, ship those with the campaign launch, populate Tier 2 in parallel with the first 5 days of outreach.

## Optional — animated screenshot loop

The hero section could embed a short looped recording (3–5 sec) of the `/spine` filtering animation: user toggles CTO → CISO lens, the metrics flip from "PRs merged 47 · review latency 4 min · queue 12" to "Vulns closed 47 · exposure window 23d → 6h · CRA Art 5 evidence ✓". Visual proof the same system serves both buyers.

Tool: `gif_creator` in Claude in Chrome — already available, would have to drive the screenshots and overlay.

Deferred — nice-to-have for v1 polish, not blocking for launch.
