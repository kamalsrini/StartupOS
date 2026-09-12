"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import Shell from "@/components/Shell";
import Tile from "@/components/Tile";
import ApprovalRow from "@/components/ApprovalRow";
import { apiGet, timeAgo, type Approval, type Cockpit } from "@/lib/api";

type AskHit = { source: string; id: string; title: string; snippet: string; url: string | null };

export default function CockpitPage() {
  const [c, setC] = useState<Cockpit | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<AskHit[] | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [ck, apr] = await Promise.all([apiGet<Cockpit>("/cockpit"), apiGet<Approval[]>("/approvals")]);
      setC(ck);
      setApprovals(apr);
    } catch (e) {
      setErr(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  async function ask() {
    if (!q.trim()) return;
    try {
      const r = await apiGet<{ hits: AskHit[] }>("/ask", { q });
      setHits(r.hits);
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <Shell
      title="Cockpit"
      crumb={c ? `${c.tenant_id} · ${c.pulse.source === "run" ? `pulse from ${c.pulse.model ?? "daemon"}` : "Tier-0 pulse (no daemon run yet)"}` : undefined}
      right={
        <button className="btn btn-ghost" onClick={load}>
          ↻ Refresh
        </button>
      }
    >
      {err ? (
        <div className="error-box">
          API unreachable: {err}. Is <code>uvicorn api.main:app --port 8000</code> running?
        </div>
      ) : null}
      {!c && !err ? <div className="muted">Loading…</div> : null}
      {c ? (
        <>
          <div className="card memory-card">
            <div className="card-header">
              <div className="card-title">
                Morning pulse <span className="accent">/ {c.pulse.source === "run" ? `Tier ${c.pulse.tier}` : "Tier 0 fallback"}</span>
              </div>
              <span className="small muted" style={{ marginLeft: 8 }}>
                {timeAgo(c.pulse.at)}
              </span>
            </div>
            <div className="card-body">
              <div className="pulse-content">{c.pulse.text}</div>
              <div className="pulse-meta">
                <span className={`pulse-pill ${c.pending_approvals ? "hot" : "cool"}`}>{c.pending_approvals} waiting</span>
                <span className="muted">· cash on hand ${c.finance.cash_on_hand} · AP 7d ${c.finance.ap_next_7d}</span>
              </div>
              <div className="ask-bar">
                <span className="muted">🜲</span>
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && ask()}
                  placeholder="Ask the OS — retrieval preview over brain docs, messages and documents"
                />
                <button onClick={ask}>Ask</button>
              </div>
              {hits ? (
                <div className="ask-output">
                  {hits.length === 0 ? (
                    <span className="muted">No matches in the brain yet.</span>
                  ) : (
                    hits.map((h) => (
                      <div className="ask-hit" key={`${h.source}:${h.id}`}>
                        <b>
                          {h.source} · {h.title}
                        </b>
                        <span dangerouslySetInnerHTML={{ __html: h.snippet }} />
                      </div>
                    ))
                  )}
                  <div className="small muted" style={{ marginTop: 6 }}>
                    Sources only — the daemon composes answers.
                  </div>
                </div>
              ) : null}
            </div>
          </div>

          <div className="cash-strip">
            {c.tiles.map((t) => (
              <Tile key={t.label} tile={t} />
            ))}
          </div>

          <div className="finance-grid">
            <div className="col">
              <div className="card">
                <div className="card-header">
                  <div className="card-title">
                    Approve to execute <span className="accent">/ {approvals.length} pending</span>
                  </div>
                </div>
                <div className="card-body">
                  <div className="trust-banner">
                    <strong>Nothing runs without you.</strong> Approve queues the row for the executor; Decline records why.
                  </div>
                  {approvals.length === 0 ? (
                    <div className="muted small">Nothing pending. The daemon adds rows when signals fire.</div>
                  ) : (
                    approvals.map((a) => <ApprovalRow key={a.id} approval={a} onDecided={() => load()} />)
                  )}
                </div>
              </div>
            </div>
            <div className="col">
              <div className="card">
                <div className="card-header">
                  <div className="card-title">
                    Quick glance <span className="accent">/ modules</span>
                  </div>
                </div>
                <div className="card-body">
                  {c.quick_glance.map((g) => (
                    <Link key={g.module} href={`/m/${g.module}`} className={`glance-row ${g.cls}`} style={{ color: "inherit" }}>
                      <span className="glance-title">{g.title}</span>
                      <span className="glance-line">{g.line}</span>
                    </Link>
                  ))}
                </div>
              </div>
              <div className="card">
                <div className="card-header">
                  <div className="card-title">
                    Your spend <span className="accent">/ {c.spend.month}</span>
                  </div>
                </div>
                <div className="card-body">
                  <div className="kv-row">
                    <span className="kv-key">Runs</span>
                    <span className="kv-val">{c.spend.runs}</span>
                  </div>
                  <div className="kv-row">
                    <span className="kv-key">Tokens</span>
                    <span className="kv-val">{c.spend.tokens.toLocaleString()}</span>
                  </div>
                  <div className="kv-row">
                    <span className="kv-key">Cost</span>
                    <span className="kv-val">${parseFloat(c.spend.cost_usd).toFixed(2)}</span>
                  </div>
                  {c.spend.by_tier.map((t) => (
                    <div className="kv-row" key={t.tier}>
                      <span className="kv-key">Tier {t.tier} · {t.runs} runs</span>
                      <span className="kv-val">${parseFloat(t.cost_usd).toFixed(2)}</span>
                    </div>
                  ))}
                  {c.spend.by_skill.slice(0, 6).map((s) => (
                    <div className="kv-row" key={`${s.skill}-${s.tier}`}>
                      <span className="kv-key">{s.skill}</span>
                      <span className="kv-val">
                        {(s.tokens_in + s.tokens_out).toLocaleString()} tok · ${parseFloat(s.cost_usd).toFixed(2)}
                      </span>
                    </div>
                  ))}
                  {c.spend.budget ? (
                    <div className="kv-row">
                      <span className="kv-key">Tier 2 budget · {c.spend.budget.state}</span>
                      <span className="kv-val">
                        {c.spend.budget.tier2_tokens_used.toLocaleString()} / {c.spend.budget.tier2_tokens_allowed.toLocaleString()}
                      </span>
                    </div>
                  ) : (
                    <div className="small muted" style={{ marginTop: 6 }}>
                      No budget set for this month — finish <Link href="/onboarding">onboarding</Link>.
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </>
      ) : null}
    </Shell>
  );
}
