"use client";

import { useCallback, useEffect, useState } from "react";
import Shell from "@/components/Shell";
import Tile from "@/components/Tile";
import ApprovalRow from "@/components/ApprovalRow";
import { apiGet, timeAgo, type Approval, type Tile as TileT } from "@/lib/api";

type Finance = {
  snapshot_at: string;
  live: boolean;
  accounts: {
    id: string;
    name: string | null;
    nickname: string | null;
    account_type: string | null;
    account_number_last_four: string | null;
    balance_breakdown: { available_balance: string };
    cashflow: { current_month_cash_inflows: string; current_month_cash_outflows: string };
  }[];
  bills: {
    id: string;
    vendor_name: string | null;
    amount: string;
    currency: string;
    status: string | null;
    payment_status: string | null;
    due_at: string | null;
    external_invoice_number: string | null;
  }[];
  vendors: { id: string; name: string; status: string | null; email: string | null; rail: string | null; country: string | null }[];
  cards: { id: string; display_name: string | null; last4: string | null; status: string | null; limit: { spent: { quantity: string }; total: { quantity: string } } }[];
  transactions: { id: string; type: string | null; status: string | null; amount: string; timestamp: string | null; display_name: string | null }[];
};
type Summary = { cash_on_hand: string; ap_next_7d: string; ap_next_7d_count: number; received_mtd: string; outflow_mtd: string; last_inflow_at: string | null; accounts: number };

const fmt = (s: string) => {
  const n = parseFloat(s.replace(/[^0-9.-]/g, ""));
  return isNaN(n) ? s : "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—");
const payPill = (p: string | null) => {
  const k = (p || "").toUpperCase();
  const cls = k === "CLEARED" || k === "SETTLED" ? "paid" : k === "SCHEDULED" ? "scheduled" : k ? "unpaid" : "draft";
  return <span className={`status-pill status-${cls}`}>{p || "—"}</span>;
};

export default function FinanceView() {
  const [f, setF] = useState<Finance | null>(null);
  const [s, setS] = useState<Summary | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [fin, sum, apr] = await Promise.all([
        apiGet<Finance>("/finance"),
        apiGet<Summary>("/finance/summary"),
        apiGet<Approval[]>("/approvals", { module: "finance" }),
      ]);
      setF(fin);
      setS(sum);
      setApprovals(apr);
    } catch (e) {
      setErr(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const tiles: TileT[] = s
    ? [
        { label: "Cash on hand", val: fmt(s.cash_on_hand), sub: `${s.accounts} Brex accounts · snapshot`, cls: "" },
        { label: "AP next 7 days", val: fmt(s.ap_next_7d), sub: `${s.ap_next_7d_count} bills due`, cls: s.ap_next_7d_count ? "warn" : "" },
        { label: "Outflow MTD", val: fmt(s.outflow_mtd), sub: "current month · Brex", cls: "" },
        { label: "Received MTD", val: fmt(s.received_mtd), sub: s.last_inflow_at ? `last inflow ${day(s.last_inflow_at)}` : "no inflows", cls: "" },
      ]
    : [];

  return (
    <Shell title="Finance" crumb="Cash · AP · Brex (read-only)" right={<button className="btn btn-ghost" onClick={load}>↻ Refresh</button>}>
      {err ? <div className="error-box">API unreachable: {err}</div> : null}
      {!f && !err ? <div className="muted">Loading…</div> : null}
      {f && s ? (
        <>
          <div className="trust-banner">
            <strong>StartupOS never moves money.</strong> Pay in Brex; this page reads a snapshot from {timeAgo(f.snapshot_at)}.
          </div>
          <div className="cash-strip">
            {tiles.map((t) => (
              <Tile key={t.label} tile={t} />
            ))}
          </div>
          <div className="finance-grid">
            <div className="col">
              <div className="card">
                <div className="card-header">
                  <div className="card-title">Bills <span className="accent">/ accounts payable</span></div>
                </div>
                <div style={{ overflowX: "auto" }}>
                  <table className="fin-table">
                    <thead>
                      <tr><th>Vendor</th><th>Invoice</th><th>Due</th><th>Payment</th><th>Amount</th></tr>
                    </thead>
                    <tbody>
                      {f.bills.length === 0 ? (
                        <tr><td colSpan={5} className="fin-empty">No bills ingested.</td></tr>
                      ) : (
                        f.bills.map((b) => (
                          <tr key={b.id}>
                            <td>{b.vendor_name || "—"}</td>
                            <td>{b.external_invoice_number || "—"}</td>
                            <td>{day(b.due_at)}</td>
                            <td>{payPill(b.payment_status)}</td>
                            <td className="amt" style={{ textAlign: "right", fontWeight: 600 }}>{fmt(b.amount)}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
              <div className="card">
                <div className="card-header">
                  <div className="card-title">Transactions <span className="accent">/ who paid us</span></div>
                </div>
                <div style={{ overflowX: "auto" }}>
                  <table className="fin-table">
                    <thead>
                      <tr><th>Date</th><th>Counterparty</th><th>Type</th><th>Amount</th></tr>
                    </thead>
                    <tbody>
                      {f.transactions.length === 0 ? (
                        <tr><td colSpan={4} className="fin-empty">No transactions ingested.</td></tr>
                      ) : (
                        f.transactions.slice(0, 25).map((t) => (
                          <tr key={t.id}>
                            <td>{day(t.timestamp)}</td>
                            <td>{t.display_name || "—"}</td>
                            <td>{t.type || "—"}</td>
                            <td style={{ textAlign: "right", fontWeight: 600, color: t.amount.startsWith("-") ? "#b91c1c" : "#166534" }}>{fmt(t.amount)}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
            <div className="col">
              <div className="card">
                <div className="card-header">
                  <div className="card-title">Approve <span className="accent">/ {approvals.length} pending</span></div>
                </div>
                <div className="card-body">
                  {approvals.length === 0 ? <div className="muted small">Nothing pending.</div> : approvals.map((a) => <ApprovalRow key={a.id} approval={a} />)}
                </div>
              </div>
              <div className="card">
                <div className="card-header"><div className="card-title">Accounts</div></div>
                <div className="card-body">
                  {f.accounts.length === 0 ? <div className="muted small">No accounts ingested.</div> : null}
                  {f.accounts.map((a) => (
                    <div className="kv-row" key={a.id}>
                      <span className="kv-key">{a.nickname || a.name} ··{a.account_number_last_four}</span>
                      <span className="kv-val">{fmt(a.balance_breakdown.available_balance)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="card">
                <div className="card-header"><div className="card-title">Vendors <span className="accent">/ no bank details</span></div></div>
                <div className="card-body">
                  {f.vendors.length === 0 ? <div className="muted small">No vendors ingested.</div> : null}
                  {f.vendors.map((v) => (
                    <div className="kv-row" key={v.id}>
                      <span className="kv-key">{v.name}{v.country ? ` · ${v.country}` : ""}</span>
                      <span className="kv-val">{v.rail || "—"}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="card">
                <div className="card-header"><div className="card-title">Cards</div></div>
                <div className="card-body">
                  {f.cards.length === 0 ? <div className="muted small">No cards ingested.</div> : null}
                  {f.cards.map((c) => (
                    <div className="kv-row" key={c.id}>
                      <span className="kv-key">{c.display_name} ··{c.last4}</span>
                      <span className="kv-val">{fmt(c.limit.spent.quantity)} / {fmt(c.limit.total.quantity)}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </>
      ) : null}
    </Shell>
  );
}
