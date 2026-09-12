---
name: finance-schema
description: "StartupOS memory schema for Finance module — bills (AP), invoices (AR), payments (incoming), cash position. Stored in localStorage under memory.finance."
type: project
---

The Finance module added these slices to the StartupOS memory schema on 2026-05-27:

```js
memory.finance = {
  cashOnHand: Number,           // manually entered or computed
  burnRate: Number,             // monthly burn, manual
  bills: [                      // AP — what we owe
    {
      id: String,
      vendor: String,
      amount: Number,
      dueDate: String (ISO),
      status: 'unpaid' | 'scheduled' | 'paid',
      category: String,         // 'infra' | 'tools' | 'contractor' | 'saas' | etc.
      notes: String,
      source: 'brex' | 'manual' | 'email',
      payUrl: String            // deep-link to where to actually pay
    }
  ],
  invoices: [                   // AR — what's owed to us
    {
      id: String,
      customer: String,
      customerEmail: String,
      amount: Number,
      issueDate: String (ISO),
      dueDate: String (ISO),
      status: 'draft' | 'sent' | 'paid' | 'overdue',
      notes: String
    }
  ],
  payments: [                   // money received
    {
      id: String,
      payer: String,
      amount: Number,
      date: String (ISO),
      method: 'wire' | 'ach' | 'card' | 'check' | 'stripe' | 'other',
      invoiceId: String (optional),
      notes: String
    }
  ]
}
```

**Computed views (not stored, derived on render):**
- AP next 7 days: sum of `bills` where `status='unpaid' AND dueDate <= +7d`
- Total AR outstanding: sum of `invoices` where `status IN ('sent','overdue')`
- Top payers rollup: group `payments` by `payer`, sum `amount`

**Why:** Per Dilbert 2026-05-27, user runs Brex for bills + spreadsheets/email for everything else. This schema becomes the spreadsheet replacement. Future integrations (Stripe, QuickBooks, Mercury) write to these same slices so the Finance UI stays stable.

**Trust contract on Finance:** StartupOS NEVER moves money. Every bill action (approve, schedule, mark paid) is a memory mutation + deep-link to Brex. User clicks Pay in Brex's own UI.

Related: [[startupos-vision]], [[startupos-reference-doc]].
