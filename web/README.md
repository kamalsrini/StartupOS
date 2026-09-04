# StartupOS web

Next.js 14 (app router, TypeScript, plain CSS) shell for onboarding, the Cockpit and the module pages. It talks to the FastAPI service only — never to Postgres, never to a model.

```
cd web
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev    # http://localhost:3000
npm run lint                                             # eslint-config-next
npm run build
```

Start the API first: `make dev-api` from the repo root (CORS allows `localhost:3000`).

## Pages

| Route | What it renders | API calls |
|---|---|---|
| `/onboarding` | Six-step Day-0 wizard: company → sources picker (9 sources, "we recommend 2") → compile with real row counts → five memory cards edit/confirm → first pulse + first approval → cadence | `POST /onboarding/tenant`, `/connections`, `/compile`, `/confirm`, `/cadence`, `GET /onboarding/status`, `GET /cockpit`, `GET /approvals` |
| `/cockpit` | Morning pulse (daemon run or Tier-0 fallback), four tiles, Ask bar (retrieval preview), pending approvals with Approve / Edit / Decline, quick glance per module, your spend | `GET /cockpit`, `GET /approvals`, `GET /ask`, `POST /approvals/{id}/decide` |
| `/m/[name]` | A `ModuleSnapshot`: memory strip, 4 tiles, signals, table(s), approvals, next integrations — same layout as the Sep 3 artifact. `name` ∈ build, marketing, web, customers, social, security, commerce, research, sales | `GET /modules/{name}` |
| `/m/finance` | BREX_SNAPSHOT view: cash tiles, bills, transactions, accounts, vendors (no bank fields), cards | `GET /finance`, `GET /finance/summary`, `GET /approvals?module=finance` |

The tenant slug is kept in `localStorage["sos.tenant"]` after `/onboarding/tenant` and sent as `?tenant=` on every request; without it the API uses its default `TENANT_ID`.

## Components

`Tile`, `SignalRow`, `ApprovalRow` (Approve / Edit preview / Decline with reason → `POST …/decide`; the API only flips the status, the daemon executes), `DataTable` (cells arrive sanitized by the API: escaped text or a single status pill / link), `Sidebar`, `Shell`.

`app/globals.css` is the artifact's visual system (`#fafaf9` ground, `#1c1917` text, 13px system font, 220px sidebar, `.card`, `.cash-tile`, `.status-pill`, `.approve-row`). It is global on purpose so the API's pill HTML renders unchanged.

## Honesty rules

Empty tables render zeros or "not connected" — the UI never invents numbers. Approve never executes anything from the browser.
