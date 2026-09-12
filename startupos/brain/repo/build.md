---
slice: build
updated: 2026-09-04
source: human
---
# Build conventions

- **Tracker:** Linear, team UnitoneSentinel. No cycles; project-based. Priority 1 = Urgent, 2 = High.
- **Deploys:** Vercel, project UNITONE (v0-unitone-website). A failed production deploy is logged to the brain; a re-push on a transient build error is the runbook default.
- **Rules:** High/Urgent issue with no assignee → propose an owner (Alexey for fixer/eval path). In Progress with no update for 7 days → nudge the owner. Duplicate titles among open issues → propose a merge.
- **Active work (Sep 2026):** multi-agent fixer verification, workspace-graph build time (24 min per parent), skill hierarchy (global/org/repo), JCI multi-repo support.
- **Known gaps:** Docker image does not auto-sync SecuritySkills on startup; pyyaml missing in the image.
