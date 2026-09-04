"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Shell from "@/components/Shell";
import Tile from "@/components/Tile";
import SignalRow from "@/components/SignalRow";
import ApprovalRow from "@/components/ApprovalRow";
import DataTable from "@/components/DataTable";
import { apiGet, timeAgo, type ModuleSnapshot } from "@/lib/api";
import FinanceView from "./finance";

export default function ModulePage() {
  const { name } = useParams<{ name: string }>();
  const [m, setM] = useState<ModuleSnapshot | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      setM(await apiGet<ModuleSnapshot>(`/modules/${name}`));
    } catch (e) {
      setErr(String(e));
    }
  }, [name]);

  useEffect(() => {
    if (name !== "finance") load();
  }, [name, load]);

  if (name === "finance") return <FinanceView />;

  const focusApproval = (id: string) => {
    setFocus(id);
    document.getElementById(`approval-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => setFocus(null), 1600);
  };

  return (
    <Shell
      title={m?.title ?? name}
      crumb={m?.crumb}
      right={
        <button className="btn btn-ghost" onClick={load}>
          ↻ Refresh
        </button>
      }
    >
      {err ? <div className="error-box">API unreachable: {err}</div> : null}
      {!m && !err ? <div className="muted">Loading…</div> : null}
      {m ? (
        <>
          <div className="card memory-card">
            <div className="card-header">
              <div className="card-title">
                Startup Memory <span className="accent">/ retrieved into {m.title}</span>
              </div>
              <span className="tag">{m.live ? "live" : "snapshot"}</span>
              <span className="small muted" style={{ marginLeft: 8 }}>
                {timeAgo(m.snapshot_at)} · {m.source}
              </span>
            </div>
            <div className="card-body">
              <div className="small" style={{ color: "#44403c" }}>
                {m.memory || <span className="muted">No memory slice yet — confirm cards in Onboarding.</span>}
              </div>
            </div>
          </div>

          <div className="cash-strip">
            {m.tiles.map((t) => (
              <Tile key={t.label} tile={t} />
            ))}
          </div>

          <div className="finance-grid">
            <div className="col">
              <div className="card">
                <div className="card-header">
                  <div className="card-title">
                    Signals <span className="accent">/ what changed</span>
                  </div>
                </div>
                <div>
                  {m.signals.length === 0 ? (
                    <div className="card-body muted small">No open signals.</div>
                  ) : (
                    m.signals.map((s) => <SignalRow key={s.id} signal={s} onFocus={focusApproval} />)
                  )}
                </div>
              </div>
              <DataTable table={m.table} />
              <DataTable table={m.table2} />
            </div>
            <div className="col">
              <div className="card">
                <div className="card-header">
                  <div className="card-title">
                    Approve to execute <span className="accent">/ {m.approvals.length} pending</span>
                  </div>
                </div>
                <div className="card-body">
                  <div className="trust-banner">
                    <strong>Nothing runs without you.</strong> Approve flips the row to approved; the daemon executes it. Decline records why.
                  </div>
                  {m.approvals.length === 0 ? (
                    <div className="muted small">Nothing pending.</div>
                  ) : (
                    m.approvals.map((a) => <ApprovalRow key={a.id} approval={a} focused={focus === a.id} />)
                  )}
                </div>
              </div>
              <div className="card">
                <div className="card-header">
                  <div className="card-title">Next integrations</div>
                </div>
                <div className="card-body">
                  <div className="stub-features">
                    {m.next.map((n) => (
                      <div key={n}>{n}</div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </>
      ) : null}
    </Shell>
  );
}
