---
name: brex-integration
description: "Brex MCP is connected for UnitOne. Tool UUID, key tools used, integration pattern for the StartupOS Finance module."
type: reference
---

**Brex MCP is connected for UnitOne** as of 2026-05-27. Authenticated as `kamal.srinivasan@unitone.ai`, ACCOUNT_ADMIN.

**MCP server UUID:** `4887702f-1c8b-43ae-a14c-8f6c7535297f`
**Tool prefix:** `mcp__4887702f-1c8b-43ae-a14c-8f6c7535297f__`

**Tools wired into the StartupOS Finance module:**
- `get_user_myself` — identify caller (header status)
- `list_business_accounts` — cash position; returns `balance_breakdown.available_balance` and `cashflow.{current_month_cash_inflows, current_month_cash_outflows}` per account
- `list_bills` — AP queue (default returns DRAFT, SUBMITTED, APPROVED, OUT_OF_POLICY, CANCELED, VOID; uses `status`, `vendor_name`, `amount`, `purchased_at`, `memo`)
- `list_vendors` — vendor enrichment (4 active vendors as of 2026-05-27, mix of contractors via INTL_SWIFT_WIRE and US_ACH)
- `list_cards` — Brex card overview (limit/spent breakdown nested in `limit.{total,spent,available}.quantity` as strings)
- `list_expenses` with `types=['CARD']` and `purchased_at_start=YYYY-MM-DD` — recent card transactions
- `list_banking_transactions` with `min_amount: 1` — incoming wires/ACH for the "who paid you" rollup; groups by `display_name`

**All `list_*` tools paginate** via `cursor` / `next_cursor`. Page size capped at 100 (default 25). When a list returns 7 items with `next_cursor: null`, that's the full set.

**Status mapping** in the artifact (raw Brex → display):
- DRAFT, CANCELED, VOID → `draft` (gray pill)
- SUBMITTED → `sent` (blue)
- OUT_OF_POLICY → `overdue` (red)
- APPROVED, SETTLED → `paid` (green)

**Brex bill statuses on this account:** All currently APPROVED (no pending bills to approve). Bill Pay flow runs entirely outside StartupOS today — when bills are submitted, they show here and the user clicks "View in Brex" to approve in Brex's own UI.

**Money never moves through StartupOS.** The Brex MCP exposes write-style tools (`upload_card_expense_receipt_from_urls`, `update_expense_memo`, `assign_limit_for_card_expenses`, `replace_attendees_for_card_expense`) — all metadata-only. There is no MCP tool for initiating a payment, which aligns with the StartupOS trust contract (see [[startupos-vision]]).

**Key data shape findings (so renderers don't break on refresh):**
- `amount` fields in `list_bills` and `list_banking_transactions` are strings like `"12500.00"` (no currency); `list_business_accounts.balance_breakdown.available_balance` is a string like `"NNN.NN USD"`. Use `parseMoneyStr()` helper in the artifact.
- `list_banking_transactions` returns positive amounts for incoming, negative for outgoing. Filter with `min_amount: 1` for incoming-only.
- `list_business_accounts.priority` distinguishes PRIMARY checking from VAULT/TREASURY.

**Live behavior of the Finance module:** loads on artifact boot via `loadBrexFinance()`. Refresh button in the Brex card panel re-fetches. No caching beyond the page session — every artifact open re-pulls from Brex.

Related: [[finance-schema]], [[startupos-vision]].
