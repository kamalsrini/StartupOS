---
slice: team
updated: 2026-09-04
source: human
---
# Team and areas of responsibility

| Person | Role | Owns |
|---|---|---|
| Kamal | CEO / engineering | Product, engineering, GTM. Final approver on every StartupOS action. |
| Alexey | Fixer / eval | The fixer + eval path (multi-agent fixer, workspace graph, skill hierarchy). Default owner for High/Urgent engineering issues in that path. |
| Manmeet | Customer POC | JCI POC delivery, customer asks, JSON issue reports. |
| Nanda | Cloud IQStudio | Cloud IQStudio project and the IQStudio product line. |

## Cadence
No cycles; project-based in Linear. Repos under UnitOneAI/* deploy on Vercel.

## Assignment heuristics (for `build.assign_owner`)
- Fixer, eval, corpus, graph, skills → Alexey.
- Customer-labelled issues (customer:*) → Manmeet.
- Cloud Studio / IQStudio → Nanda.
