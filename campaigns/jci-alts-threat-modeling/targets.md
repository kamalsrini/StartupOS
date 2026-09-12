# Targets — JCI Alts · Threat Modeling Wave 2.0

18 accounts. ICP score 0–100 (Firmographic 40 + Technographic 30 + Intent 30, same model as the existing scorer.py).

**Approval state:** Draft — awaiting your morning review. Mark each row APPROVE / SKIP / DEFER.

---

## Tier 1 — Major BMS / BAS vendors

| Rank | Company | Products to reference | Persona to target | ICP | Hook |
|---|---|---|---|---|---|
| 1 | **Siemens Smart Infrastructure** | Desigo CC v9, Desigo Optic v5.2, SLX controllers, Building X | VP Product Security · VP Eng (Building X) | 92 | "Building X just landed at AHR 2026. We've been profiling Desigo CC's BACnet stack — the BACpypes integration shows up in our threat model with 4 crit findings before code lands. 15 min on what we're seeing." |
| 2 | **Schneider Electric — Buildings** | EcoStruxure Building Operation, EcoStruxure Building Advisor | VP Product Security · Head of Buildings Engineering | 90 | "EcoStruxure's BACnet implementation matches the upstream stack we've already shown 12 threats on in /spine. We'd run the same agentic threat model on EBO source under NDA — 4-hour turnaround. Worth a look?" |
| 3 | **Carrier Global / Automated Logic** | WebCTRL, ALC controllers | VP Product Security · CTO Automated Logic | 87 | "Automated Logic just acquired Logical Building Automation in May. New code, new threat surface. We can run the synthesis pass over the combined codebase before the integration ships." |
| 4 | **Trane Technologies** | Tracer SC+, Tracer Concierge | VP Product Security · Head of Engineering Tracer | 85 | "Tracer SC+ uses BACnet/IP and there are 3 unpatched BACnet protocol stack CVEs we're already showing in our demo. We'd like to give your PSIRT a private heads-up before publishing." |
| 5 | **ABB Smart Buildings** | i-bus, Cylon (acquired) | Head of Cyber Security · Head of Smart Buildings Tech | 80 | "Cylon's BACnet implementation is on our roadmap to profile. We can show you what we find before the public disclosure window opens." |
| 6 | **Mitsubishi Electric — Building Automation** | MELCloud, AE-200 | VP Product Security · Director of Building Solutions Engineering | 75 | "BACnet stacks in MELCloud and AE-200 use upstream libraries we've already shown 12 threats on. PSIRT briefing offer." |
| 7 | **Daikin Applied** | Daikin Intelligent Equipment, Daikin One+ | Head of Product Security · VP Engineering, Applied | 72 | "Intelligent Equipment uses BACnet/IP. We're already showing CVEs in two upstream libraries you depend on. 15-min PSIRT brief." |
| 8 | **Honeywell Building Technologies (incl. Tridium/Niagara)** | Niagara Framework, IQ4x, Forge, EBI | VP Product Security Niagara · Head of Tridium Engineering | 95 | **⚠ HOLD** — conflict-of-interest risk. We're literally finding bugs in their products in /spine. Better path: warm intro via advisory network. Do NOT cold-email this account in this wave. |

## Tier 2 — Pure-play BAS vendors

| Rank | Company | Products to reference | Persona to target | ICP | Hook |
|---|---|---|---|---|---|
| 9 | **Distech Controls (Acuity Brands)** | EC-Net Pro, ECLYPSE controllers | Head of Product Security · VP Eng | 78 | "ECLYPSE is BACnet/IP. Upstream BACnet stack CVEs are in /spine. Worth a heads-up before they hit your customers." |
| 10 | **Delta Controls (Delta Electronics)** | enteliWEB, ORCAview, O3 sensors | VP Engineering · Head of Product Security | 75 | "enteliWEB BACnet implementation overlaps with the upstream stack we've already shown 4 crit findings on. /spine demo runs in 4 min." |
| 11 | **KMC Controls** | KMC Conquest, BAC controllers | CTO · Head of Cyber | 70 | "BACnet IP stack in KMC Conquest — we've already shown findings on the same upstream library. PSIRT briefing 15 min." |
| 12 | **Reliable Controls** (Canadian, independent) | MACH-System, RC-Studio | VP Engineering · Director of Cyber | 68 | "BACnet implementations in MACH-System overlap with our upstream findings. Quick private briefing offer." |
| 13 | **Cylon Auto-Matrix (Acuity Brands)** | Aspect, UnitronUC32 | Head of Product Security (parent: Acuity) · VP Eng | 65 | "Same parent as Distech — likely shared PSIRT. Use single Acuity touch with both products surfaced." |
| 14 | **Crestron Electronics** | DigitalMedia, NVX, Fusion | VP Product Security · CTO | 60 | "Crestron's commercial control surface is adjacent — useful for the multi-tenant blast-radius story even if not core BMS." |
| 15 | **Lutron Electronics** | Quantum, Athena, RadioRA | VP Engineering · Head of Cyber | 60 | "Lutron Quantum BACnet integration shows up in our `/spine` demo at NVD 2018-04-23. Specific CVE walk-through offer." |

## Tier 3 — Adjacent automation (deferred)

| Rank | Company | Products to reference | Persona to target | ICP | Hook |
|---|---|---|---|---|---|
| 16 | **Belimo** | Cloud-connected actuators, energy valves | Head of Product Security · CTO | 55 | "Adjacent — actuator + valve telemetry. /spine fleet view includes BACnet-talking device class." |
| 17 | **Ingersoll Rand** (industrial side, post-Trane spinoff) | Compressors w/ industrial controls | Head of Product Security | 50 | "Adjacent — industrial controls overlap. Defer until Tier 1 sees first reply." |
| 18 | **Rockwell Automation** | FactoryTalk, Logix controllers | VP Cyber · CTO of Software | 48 | "Industrial side, but increasingly building-system-adjacent. Defer." |

---

## Scoring rationale (firmographic + technographic + intent)

- **Firmographic (max 40):** size of BMS/BAS business unit, number of shipping products, public CVE history, dedicated PSIRT
- **Technographic (max 30):** uses BACnet/IP, ships firmware/OS images, has cloud-connected device fleet, runs bug bounty
- **Intent (max 30):** recent CVE disclosures, recent PSIRT hires (Apollo job postings), CRA Article 5 readiness posts, AHR Expo presence

Tier 1 averages ~85; Tier 2 averages ~70; Tier 3 averages ~50.

## Wave size recommendation

- Start with **Tier 1 (7 accounts ex-Honeywell)** = 7 × ~3 contacts per account = ~21 contacts in Apollo, well under the 25/day cap
- After 5 business days with at least 1 reply or 3 clicks, expand to Tier 2 (7 × 3 = 21 more)
- Tier 3 deferred indefinitely

## Contact discovery (where to find personas)

For each target, use Apollo Mixed People Search with:
- Title contains: "Product Security", "Application Security", "VP Engineering", "Director of Engineering", "PSIRT", "CISO" (only if no other persona found)
- Department: Engineering OR Security
- Company size: 5000+ employees (Tier 1) or 500+ (Tier 2)
- Exclude: Sales, Marketing, HR, Finance

Where Apollo returns multiple candidates, prioritize the one with:
1. Most LinkedIn activity in last 90 days (active = responsive)
2. Published any security blog/talk (technical credibility — will appreciate our angle)
3. Has the product name in their headline (signals direct ownership)

This is exactly what `messenger.py` plus the `gtm-enrich-and-score` skill does today. Wire-up should be straightforward.
