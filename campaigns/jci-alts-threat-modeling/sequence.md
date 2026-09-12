# Sequence — JCI Alts · Threat Modeling Wave 2.0

**Persona:** VP Product Security / Head of Product Security (primary)
**Voice:** Direct, technical, no consultant-speak. Lead with specifics — CVE numbers, exact compliance dates, named systems. Earn the engineer's respect first.
**Length:** Touch 1 ≤ 90 words, Touch 2 ≤ 60 words, Touch 3 ≤ 120 words, Touch 4 ≤ 50 words.
**Total spend:** 4 emails + 2 LinkedIn over 12 calendar days, ~25 minutes of buyer attention if read in full.
**Approval gate:** Each draft below is a template. Personalize {COMPANY}, {PRODUCT_REFERENCE}, {SPECIFIC_FINDING}, {SENDER_FIRST_NAME} before sending. Nothing sends until you approve.

---

## Touch 1 — Cold open (Day 0)

**Subject A:** `{N} CVEs in {PRODUCT_REFERENCE} before NVD did`
**Subject B:** `private heads-up on {PRODUCT_REFERENCE}`
**Subject C:** `BACnet stack — what we're seeing`

**Body:**

```
{first_name},

I run product security tooling at UnitOne. We're building an automated threat-modeling-to-remediation loop — synthesis pass on protocol stacks, agentic threat models per design surface, fix confidence based on combined scanner agreement + callgraph reach + skill match.

Quick reason for the reach-out: in our demo we're already showing live CVEs in {PRODUCT_REFERENCE} and the upstream BACnet stack you build on. {SPECIFIC_FINDING_ONE_LINE}.

Worth giving your PSIRT a private walk-through of what we've surfaced. 15 minutes, no pitch.

— {SENDER_FIRST_NAME}

→ Full /spine demo with your fleet profile: {LANDING_URL}
```

**Personalization fills:**
- `{PRODUCT_REFERENCE}` = the most specific product in their portfolio (e.g., Siemens Desigo CC, Schneider EcoStruxure, Carrier WebCTRL, Trane Tracer SC+)
- `{SPECIFIC_FINDING_ONE_LINE}` = one CVE or one threat-model finding, max one line. Examples:
  - Siemens: "Desigo CC's BACpypes integration returns 4 crit / 4 high / 4 med in our synthesis pass, score 0 — untriaged."
  - Schneider: "EcoStruxure's BACnet implementation overlaps with the upstream stack we've shown 12 threats on."
  - Carrier: "Automated Logic + Logical Building Automation integration — combined codebase synthesis available."
- `{LANDING_URL}` = the per-account landing page with UTM tag (`?utm_source=outbound&utm_medium=email&utm_campaign=jci-alts-tm-v1&utm_content=t1`)

**Voice check:** No "AI-powered", no "transform", no "leverage". The phrase "PSIRT" should land because that's their world.

---

## LinkedIn-1 — Connect request (Day 2)

**Connect note (≤ 280 chars):**

```
{first_name} — reaching out because {COMPANY}'s {PRODUCT_REFERENCE} shows up in our threat-model demo. Specifically: {ONE_FACT}. Sent you a private note. No pitch in the connect — just want to be in your network in case it's useful down the line. — {SENDER_FIRST_NAME}
```

**Personalization:**
- `{ONE_FACT}` = different from Touch 1's `{SPECIFIC_FINDING_ONE_LINE}` — a second concrete signal. E.g., for Schneider: "EcoStruxure was flagged in CISA KEV for a related issue on 2026-03-12."

**Exit if:** Already a connection. Don't re-send.

---

## Touch 2 — Reply-to-prior, new angle (Day 3)

**Subject:** Re: {Subject from T1} ← keep reply chain

**Body:**

```
Following up — meant to share this.

Your competitor {ADJACENT_VENDOR} just patched a similar BACnet path-traversal in {ADJACENT_PRODUCT} ({PATCH_DATE}). The same pattern is in our /spine demo for {PRODUCT_REFERENCE}. Happy to send the specific CVSS calc walk-through.

— {SENDER_FIRST_NAME}
```

**Personalization:**
- `{ADJACENT_VENDOR}` / `{ADJACENT_PRODUCT}` — pick the closest competitor who recently patched something concrete. E.g., for Siemens: "Honeywell" / "Niagara N4". For Carrier: "Schneider" / "EcoStruxure Building Operation". For Distech: "Delta Controls" / "enteliWEB". If no recent patch, swap to: "We also surfaced the same pattern in our research database. Walk-through?"

**Exit if:** Replied to T1. (handled by `engagement_watcher.py` → pause)

---

## Touch 3 — Concrete proof (Day 7)

**Subject:** `the CVSS calc on the {PRODUCT_REFERENCE} finding`

**Body:**

```
{first_name} — landing one more.

Here's the actual scoring on the {PRODUCT_REFERENCE} finding from our /spine demo:

  Reachable: Yes · prod (callgraph traced)
  CVSS Base v4: 9.3 critical
  EPSS: 47% (30-day exploit probability)
  KEV: Listed (CISA · 2026-03)
  Fix confidence: 94% — combined signal: scanner agreement × callgraph reach × skill match × differential verify

The agentic loop on this one would open a draft PR with a skill-validated patch within 4 minutes of finding ingest. That's the closed loop we'd want to show your team in 15 minutes.

If your PSIRT or product security team would like a private walk-through over the demo with your products loaded — reply with a time and I'll send the link.

— {SENDER_FIRST_NAME}

→ Same /spine view, your fleet profile pre-loaded: {LANDING_URL}
```

**Voice check:** Numbers, vendor terms (PSIRT, callgraph, CVSS v4, KEV), short paragraphs. No emojis. No exclamation marks. No "I'd love to".

---

## LinkedIn-2 — DM (Day 9)

**Only fires if there was a click or multi-open signal between T1 and T7.** This is the path `linkedin_followup_queue.py` already handles — adapt to this campaign by referencing the threat-modeling angle.

**DM body (≤ 400 chars):**

```
{first_name} — saw you clicked through to the /spine demo last week. The specific finding that triggered that was {SPECIFIC_FINDING_LINK}. Worth a 15-min walk-through with your team? Happy to come on Webex/Teams/Zoom — whatever works. — {SENDER_FIRST_NAME}
```

**Exit if:** Already booked a meeting. Already paused.

---

## Touch 4 — Break-up (Day 12)

**Subject:** `closing the loop`

**Body:**

```
{first_name},

Closing the loop on this thread — won't keep emailing.

If timing's off, no problem. If you'd like a heads-up when we ship the next batch of findings in BACnet stacks and {PRODUCT_REFERENCE}, reply with "stay in touch" and I'll add you to the quarterly summary.

Either way — appreciate the read.

— {SENDER_FIRST_NAME}
```

**Voice check:** Clean exit. No guilt-trip. No "did you see my last email". One concrete option ("stay in touch" reply tag) for low-effort future opt-in.

---

## Persona variants (notes — not full drafts)

**VP Engineering / CTO variant:**
- Lead with engineering velocity, not security
- Replace "PSIRT" with "engineering team" or "platform team"
- Touch 1 hook: "we found 12 threats in your protocol stack before code lands" — frame as design-tier, not detection
- Touch 3 metrics: emphasize fix-confidence + 4-min-to-PR; de-emphasize CVSS/KEV
- Same 4-touch cadence, same exit conditions

**CISO variant (only if no other persona resolvable):**
- Lead with regulatory: CRA Article 5, SOC2 CC6.6
- Reference the existing Wave 1 CRA OT/BAS messaging (consistency across waves at the same account)
- Touch 1 hook: "Your fleet's CRA Art 5 readiness — we have the evidence pack pattern"
- This variant overlaps with Wave 1; only use if Wave 1 did not engage them and persona-resolution couldn't find a Product Security lead

---

## Apollo sequence setup checklist

When you approve in the morning, the setup steps are:

1. Create new Apollo sequence: `JCI Alts — Threat Modeling v1`
2. 4 steps, intervals: Day 0, Day 3, Day 7, Day 12
3. Track opens + clicks (existing Apollo config — see [[unitone-gtm-stack]] for UTM_Taxonomy)
4. Skip-weekends: ON
5. Pause-on-reply: ON
6. Pause-on-bounce: ON
7. Daily send cap: 25 (matches existing `auto_enroll.py` cap)
8. Reply detection routes to Slack via `engagement_watcher.py` (already configured)

The existing automation handles the rest — `linkedin_followup_queue.py` will pick up the LI-2 trigger when a click is detected.

## Hand-off to StartupOS

Once approved, the sequence shows up in the StartupOS Sales module's **Active Sequences** card alongside VP Engineering, CTO v2, and CRA OT/BAS. The Engagement Signals panel will route 🔴/🟠/🟡/⚪ alerts for this campaign the same way as the others.
