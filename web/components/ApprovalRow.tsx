"use client";

import { useState } from "react";
import { ApiError, decide, type Approval } from "@/lib/api";

export default function ApprovalRow({
  approval,
  focused,
  onDecided,
}: {
  approval: Approval;
  focused?: boolean;
  onDecided?: (a: Approval) => void;
}) {
  const [a, setA] = useState(approval);
  const [editing, setEditing] = useState(false);
  const [preview, setPreview] = useState(approval.preview);
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run(decision: "approve" | "decline") {
    setBusy(true);
    setErr(null);
    try {
      const extra: { reason?: string; edited_preview?: string } = {};
      if (preview !== a.preview) extra.edited_preview = preview;
      if (decision === "decline" && reason) extra.reason = reason;
      const updated = await decide(a.id, decision, extra);
      setA(updated);
      setEditing(false);
      setDeclining(false);
      onDecided?.(updated);
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  const done = a.status !== "pending";
  return (
    <div className={`approve-row${done ? " done" : ""}${focused ? " focus" : ""}`} id={`approval-${a.id}`}>
      <div className="approve-head">
        <span className="approve-type">{a.type}</span>
        <span className="approve-target">{a.target}</span>
      </div>
      {editing ? (
        <textarea
          className="approve-preview editing"
          style={{ width: "100%", minHeight: 90, resize: "vertical" }}
          value={preview}
          onChange={(e) => setPreview(e.target.value)}
        />
      ) : (
        <div className="approve-preview">{a.status === "pending" ? preview : a.preview}</div>
      )}
      {a.exec ? (
        <div className="approve-exec">
          exec → {a.exec.server}.{a.exec.tool}
        </div>
      ) : (
        <div className="approve-exec">record-only · nothing runs</div>
      )}
      <div className="approve-actions">
        {done ? (
          <span className="small" style={{ color: a.status === "declined" ? "#b91c1c" : "#166534", fontWeight: 600 }}>
            {a.status === "approved" && "✓ Approved · queued for the executor"}
            {a.status === "declined" && `✕ Declined${a.decline_reason ? ` · ${a.decline_reason}` : ""}`}
            {a.status === "executed" && "✓ Executed"}
            {a.status === "failed" && "✕ Failed"}
          </span>
        ) : declining ? (
          <>
            <input
              className="decline-reason"
              placeholder="Why? (feeds the brain as a preference)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <button className="btn btn-decline" disabled={busy} onClick={() => run("decline")}>
              Confirm decline
            </button>
            <button className="btn btn-ghost btn-edit" onClick={() => setDeclining(false)}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-approve" disabled={busy} onClick={() => run("approve")}>
              Approve
            </button>
            <button className="btn btn-edit" onClick={() => setEditing((v) => !v)}>
              {editing ? "Done" : "Edit"}
            </button>
            <button className="btn btn-decline" onClick={() => setDeclining(true)}>
              Decline
            </button>
          </>
        )}
        {err ? <span className="small" style={{ color: "#b91c1c" }}>{err}</span> : null}
      </div>
    </div>
  );
}
