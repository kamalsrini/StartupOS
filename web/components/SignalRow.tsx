import type { SignalView } from "@/lib/api";

const ICON = { reply: "!", click: "→", open: "◔", bounce: "·" } as const;

export default function SignalRow({ signal, onFocus }: { signal: SignalView; onFocus?: (approvalId: string) => void }) {
  const s = signal;
  let action: React.ReactNode = null;
  if (s.action && s.approval_id && onFocus) {
    action = (
      <button className="signal-action" onClick={() => onFocus(s.approval_id as string)}>
        {s.action}
      </button>
    );
  } else if (s.action && s.href) {
    action = (
      <a className="signal-action" href={s.href} target="_blank" rel="noopener noreferrer">
        {s.action}
      </a>
    );
  }
  return (
    <div className="signal-row">
      <div className={`signal-icon ${s.kind}`}>{ICON[s.kind] ?? "·"}</div>
      <div className="signal-body">
        <div className="signal-title">{s.title}</div>
        <div className="signal-meta">{s.meta}</div>
      </div>
      {action}
    </div>
  );
}
