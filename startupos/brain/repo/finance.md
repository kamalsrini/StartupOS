---
slice: finance
updated: 2026-09-04
source: human
---
# Finance conventions

- **Bank:** Brex. Primary checking is the operating account; the vault is non-primary. StartupOS reads Brex, never moves money — payments are approved by clicking Pay inside Brex via deep link.
- **AP rule:** any bill not CLEARED/SETTLED and due within 7 days is a High signal; the AP queue lists them with Brex deep links.
- **Runway rule:** primary available balance below 3x average monthly outflow is a High signal.
- **Inflows:** an incoming wire with no matching invoice or known account is flagged Low for a manual match.
- **Vendors:** stored as id, name, email, rail, country, status only. Bank and tax details never leave Brex.
- **Contractors:** paid by ACH (US) or SWIFT wire (international); invoices carry an external invoice number.
