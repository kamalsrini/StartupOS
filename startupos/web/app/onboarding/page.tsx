"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import Shell from "@/components/Shell";
import Tile from "@/components/Tile";
import ApprovalRow from "@/components/ApprovalRow";
import { ALLOW_BOOTSTRAP, apiGet, apiPost, me, type Approval, type Cockpit, type JobView, type MemoryCard, type OnboardingStatus } from "@/lib/api";
import s from "./wizard.module.css";

const STEPS = ["Company", "Sources", "Compile", "Memory", "First pulse", "Cadence"] as const;
const SOURCES: { id: string; name: string; sub: string; ref: string }[] = [
  { id: "linear", name: "Linear", sub: "issues, projects", ref: "env:LINEAR_API_KEY" },
  { id: "slack", name: "Slack", sub: "channels, threads", ref: "env:SLACK_BOT_TOKEN" },
  { id: "brex", name: "Brex", sub: "cash, bills, cards (read-only)", ref: "env:BREX_API_TOKEN" },
  { id: "apollo", name: "Apollo", sub: "sequences, accounts", ref: "env:APOLLO_API_KEY" },
  { id: "github", name: "GitHub", sub: "PRs, commits", ref: "env:GITHUB_TOKEN" },
  { id: "gdrive", name: "Google Drive", sub: "docs", ref: "env:GDRIVE_TOKEN" },
  { id: "gmail", name: "Gmail", sub: "threads", ref: "env:GMAIL_TOKEN" },
  { id: "vercel", name: "Vercel", sub: "deployments", ref: "env:VERCEL_TOKEN" },
  { id: "stripe", name: "Stripe", sub: "subscriptions (read-only)", ref: "env:STRIPE_API_KEY" },
];
const SLICE_LABEL: Record<MemoryCard["slice"], string> = { identity: "Identity", icp: "ICP", voice: "Brand voice", pricing: "Pricing & offer", team: "Team" };

type TenantResp = { tenant: { id: string; name: string; website: string | null }; recommended_sources: string[] };
type CompileResp = { cards: MemoryCard[]; counts: Record<string, number>; outcome: string; jobs: (JobView & { enqueued: boolean })[] };
type CardsResp = { cards: MemoryCard[]; counts: Record<string, number> };
type Cred = { mode: "key" | "ref"; value: string };
const POLL_MS = 3000;

function jobLabel(j: JobView): string {
  if (j.source) return `Backfill ${SOURCES.find((x) => x.id === j.source)?.name ?? j.source}`;
  return { signals: "Signals", context_pack: "Context pack", chief_of_staff: "Chief of Staff", morning_pulse: "Morning pulse" }[j.kind] ?? j.kind;
}
function jobMeta(j: JobView): string {
  if (j.status === "done" && j.result && j.source) {
    const parts = Object.entries(j.result as Record<string, { created?: number }>).map(([t, r]) => `${r?.created ?? 0} ${t}`);
    return parts.length ? `read ${parts.join(", ")}` : "done";
  }
  if (j.status === "done") return "done";
  if (j.status === "running") return "running…";
  if (j.status === "failed") return `failed after ${j.attempts} attempts`;
  return j.attempts > 0 ? `retry ${j.attempts + 1} of 3` : "queued";
}
const chainActive = (st: OnboardingStatus | null) => !!st && st.jobs.queued + st.jobs.running > 0;

export default function OnboardingPage() {
  const [step, setStep] = useState(0);
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // step 1
  const [name, setName] = useState("");
  const [website, setWebsite] = useState("");
  const [email, setEmail] = useState("");
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [recommended, setRecommended] = useState<string[]>(["linear", "slack"]);
  // step 2
  const [picked, setPicked] = useState<Record<string, Cred>>({});
  const [connected, setConnected] = useState<string[]>([]);
  // step 3
  const [compiled, setCompiled] = useState<CompileResp | null>(null);
  // step 4
  const [cards, setCards] = useState<MemoryCard[]>([]);
  const [confirmed, setConfirmed] = useState<string[]>([]);
  // step 5
  const [cockpit, setCockpit] = useState<Cockpit | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  // step 6
  const [tz, setTz] = useState("America/Los_Angeles");
  const [hour, setHour] = useState(7);
  const [channel, setChannel] = useState<"web" | "slack" | "both">("web");
  // Seeded from the tenant's own ceiling once status arrives (below): a self-serve company's monthly allowance
  // is far smaller than the founder default, and offering a number the API will refuse is not a form.
  const [budget, setBudget] = useState(1500000);
  const [budgetCeiling, setBudgetCeiling] = useState<number | null>(null);

  // Sprint 3d (Track G): a founder who just signed up with Google arrives here with step 1 already done (the
  // company was created on the sign-in page, from their verified Google identity). Landing them on a "Name the
  // company" form they cannot submit reads like a broken product, so the first status we see picks up where
  // they actually are. Only once, and only forwards — it must never yank someone off a step they chose.
  const jumped = useRef(false);
  const refresh = useCallback(async () => {
    // Status is per signed-in user; a 401 here just means "not signed up yet" — don't bounce to /login.
    if (!(await me().catch(() => null))) return;
    try {
      const st = await apiGet<OnboardingStatus>("/onboarding/status");
      setStatus(st);
      setConfirmed(st.cards_confirmed);
      if (typeof st.tier2_tokens_ceiling === "number") {
        const ceiling = st.tier2_tokens_ceiling;
        setBudgetCeiling(ceiling);
        setBudget((b) => (b > ceiling ? ceiling : b));
      }
      if (!jumped.current) {
        jumped.current = true;
        const done = [st.steps.tenant, st.steps.connections, st.steps.compiled, st.steps.cards, st.steps.pulse, st.steps.cadence];
        const first = done.indexOf(false);
        if (done[0] && first > 0) setStep(first);
      }
    } catch (e) {
      setErr(String(e));
    }
  }, []);
  useEffect(() => {
    try {
      setTz(Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Los_Angeles");
    } catch {
      /* keep default */
    }
    refresh();
  }, [refresh]);

  // The first-pulse chain runs in the daemon; poll /onboarding/status while any job is queued or running (and,
  // on the pulse step, until the pulse run exists). When the backfill lands, re-read the cards read-only.
  const wasActive = useRef(false);
  useEffect(() => {
    const waiting = chainActive(status) || (step === 4 && !!status && !status.first_pulse_ready);
    if (chainActive(status)) wasActive.current = true;
    if (!waiting) {
      if (wasActive.current && status && compiled) {
        wasActive.current = false;
        apiGet<CardsResp>("/onboarding/cards").then((r) => { setCompiled((c) => (c ? { ...c, cards: r.cards, counts: r.counts } : c)); setCards(r.cards); }).catch(() => {});
        if (step === 4) apiGet<Cockpit>("/cockpit").then(setCockpit).catch(() => {});
      }
      return;
    }
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [status, step, compiled, refresh]);

  async function guard<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setErr(null);
    try {
      return await fn();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const stepDone = (i: number) =>
    status ? [status.steps.tenant, status.steps.connections, status.steps.compiled, status.steps.cards, status.steps.pulse, status.steps.cadence][i] : false;

  return (
    <Shell title="Onboarding" crumb="Day 0 · opens with work already done">
      <div className={s.wrap}>
        <div className={s.steps}>
          {STEPS.map((label, i) => (
            <span key={label} className={`${s.step} ${i === step ? s.active : ""} ${stepDone(i) ? s.done : ""}`} onClick={() => setStep(i)}>
              {i + 1}. {label}
            </span>
          ))}
          {status ? (
            <span className="small muted" style={{ alignSelf: "center" }}>
              {status.done} / {status.total} done · tenant {status.tenant_id}
            </span>
          ) : null}
        </div>
        {err ? <div className="error-box">{err}</div> : null}

        {step === 0 ? (
          <div className="card">
            <div className="card-header"><div className="card-title">Name the company</div></div>
            <div className="card-body">
              <div className={s.row}>
                <div className={s.field}><label className={s.label}>Company</label><input value={name} onChange={(e) => setName(e.target.value)} placeholder="UnitOne" /></div>
                <div className={s.field}><label className={s.label}>Website</label><input value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="unitone.ai" /></div>
              </div>
              <div className={s.field}><label className={s.label}>Your email (owner)</label><input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" /></div>
              {ALLOW_BOOTSTRAP ? (
                <div className={s.field}><label className={s.label}>Bootstrap token (STARTUPOS_BOOTSTRAP_TOKEN)</label><input type="password" value={bootstrapToken} onChange={(e) => setBootstrapToken(e.target.value)} /></div>
              ) : (
                <div className={s.hint}>Sign-up happens on the sign-in page: <a href="/login">continue with Google</a> and, if that address has no account yet, you are offered a new company of your own. This form is for operator installs that use a bootstrap token.</div>
              )}
              <div className={s.hint}>Behind it: tenant created, five brain slices seeded as drafts. No model call happens here.</div>
              <div className={s.actions}>
                <button
                  className="btn btn-primary"
                  disabled={busy || !name.trim() || !email.trim() || !bootstrapToken}
                  onClick={() =>
                    guard(async () => {
                      const r = await apiPost<TenantResp>("/onboarding/tenant", { name, website, email, bootstrap_token: bootstrapToken });
                      setBootstrapToken("");
                      setRecommended(r.recommended_sources);
                      await refresh();
                      setStep(1);
                    })
                  }
                >
                  Create tenant →
                </button>
                {status?.steps.tenant ? <span className={s.ok}>✓ tenant {status.tenant_id}</span> : null}
              </div>
            </div>
          </div>
        ) : null}

        {step === 1 ? (
          <div className="card">
            <div className="card-header"><div className="card-title">Connect two sources <span className="accent">/ we recommend {recommended.map((r) => SOURCES.find((x) => x.id === r)?.name ?? r).join(" + ")}</span></div></div>
            <div className="card-body">
              <div className={s.sources}>
                {SOURCES.map((src) => (
                  <button
                    key={src.id}
                    className={`${s.source} ${picked[src.id] !== undefined ? s.on : ""} ${recommended.includes(src.id) ? s.rec : ""}`}
                    onClick={() =>
                      setPicked((p) => {
                        const n = { ...p };
                        if (n[src.id] !== undefined) delete n[src.id];
                        else n[src.id] = { mode: "key", value: "" };
                        return n;
                      })
                    }
                  >
                    <span className={s.sourceName}>{src.name}{connected.includes(src.id) || status?.steps.connections ? " ✓" : ""}</span>
                    <span className={s.sourceSub}>{src.sub}</span>
                  </button>
                ))}
              </div>
              {Object.keys(picked).map((id) => (
                <div className={s.field} key={id}>
                  <label className={s.label}>{SOURCES.find((x) => x.id === id)?.name} · {picked[id].mode === "key" ? "API key" : "secret reference"}</label>
                  <div className={s.credMode}>
                    <button type="button" className={picked[id].mode === "key" ? s.on : ""} onClick={() => setPicked((p) => ({ ...p, [id]: { mode: "key", value: "" } }))}>Paste the key</button>
                    <button type="button" className={picked[id].mode === "ref" ? s.on : ""} onClick={() => setPicked((p) => ({ ...p, [id]: { mode: "ref", value: SOURCES.find((x) => x.id === id)?.ref ?? "" } }))}>Operator reference</button>
                  </div>
                  <input
                    type={picked[id].mode === "key" ? "password" : "text"}
                    autoComplete="off"
                    value={picked[id].value}
                    onChange={(e) => setPicked((p) => ({ ...p, [id]: { ...p[id], value: e.target.value } }))}
                    placeholder={picked[id].mode === "key" ? "your API key — encrypted for this company only" : "env:NAME or kv:NAME"}
                  />
                </div>
              ))}
              <div className={s.hint}>A pasted key is encrypted at rest for this company only and never shown again (the API returns only whether one is held). A reference (env:… or kv:…) points at a key the operator manages.</div>
              <div className={s.actions}>
                <button
                  className="btn btn-primary"
                  disabled={busy || Object.keys(picked).length === 0 || Object.values(picked).some((c) => !c.value.trim())}
                  onClick={() =>
                    guard(async () => {
                      for (const [source, cred] of Object.entries(picked)) {
                        const body = cred.mode === "key" ? { source, credential: cred.value, config: {} } : { source, secret_ref: cred.value, config: {} };
                        await apiPost("/onboarding/connections", body);
                      }
                      setConnected((c) => Array.from(new Set([...c, ...Object.keys(picked)])));
                      setPicked({}); // never keep a pasted key in memory longer than the request
                      await refresh();
                    })
                  }
                >
                  Save connections
                </button>
                <button className="btn" disabled={!status?.steps.connections} onClick={() => setStep(2)}>Next →</button>
                <span className="small muted">{status?.connections ?? 0} connected · need 2</span>
              </div>
            </div>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="card">
            <div className="card-header"><div className="card-title">The brain compiles <span className="accent">/ and shows its work</span></div></div>
            <div className="card-body">
              <div className={s.hint}>Compile returns the five cards at once and queues the first-pulse chain for the daemon: backfill each connected source, run the signal rules, compile the context pack, the Chief of Staff, then the morning pulse. Progress below is live.</div>
              {status && status.jobs.chain.length > 0 ? (
                <div className={s.chain}>
                  {status.jobs.chain.map((j) => (
                    <div key={j.id} className={`${s.job} ${s[j.status]}`}>
                      <span className={s.jobDot} />
                      <span className={s.jobName}>{jobLabel(j)}</span>
                      <span className={s.jobMeta}>{jobMeta(j)}</span>
                      {j.status === "failed" && j.error ? <span className={s.jobError}>{j.error}</span> : null}
                    </div>
                  ))}
                  <div className="small muted">
                    {chainActive(status) ? "Working… the chain continues past a failed backfill; the pulse runs on whatever landed." : status.first_pulse_ready ? "First pulse ready." : status.jobs.failed ? "Chain finished with failures — reconnect the source and compile again." : "Idle."}
                  </div>
                </div>
              ) : null}
              {compiled ? (
                <>
                  <div className={s.counts}>
                    {Object.entries(compiled.counts).map(([k, v]) => (
                      <div className={s.count} key={k}><div className={s.countVal}>{v}</div><div className={s.countKey}>{k}</div></div>
                    ))}
                  </div>
                  <div className="small" style={{ color: "#44403c" }}>{compiled.outcome}</div>
                </>
              ) : null}
              <div className={s.actions}>
                <button
                  className="btn btn-primary"
                  disabled={busy || chainActive(status)}
                  onClick={() =>
                    guard(async () => {
                      const r = await apiPost<CompileResp>("/onboarding/compile");
                      setCompiled(r);
                      setCards(r.cards);
                      await refresh();
                    })
                  }
                >
                  {chainActive(status) ? "Compiling…" : compiled || status?.steps.compiled ? "Compile again" : "Compile"}
                </button>
                <button className="btn" disabled={!compiled} onClick={() => setStep(3)}>Review five cards →</button>
                {status ? <span className="small muted">{status.jobs.done} done · {status.jobs.queued + status.jobs.running} pending · {status.jobs.failed} failed</span> : null}
              </div>
            </div>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="card">
            <div className="card-header"><div className="card-title">Confirm the five memory cards <span className="accent">/ {confirmed.length} of 5</span></div></div>
            <div className="card-body">
              {cards.length === 0 ? <div className="muted small">Run Compile first.</div> : null}
              <div className={s.cards}>
                {cards.map((c, i) => (
                  <div className={s.memCard} key={c.slice}>
                    <div className={s.memHead}>
                      <span className={s.memSlice}>{SLICE_LABEL[c.slice]}</span>
                      {confirmed.includes(c.slice) ? <span className={s.ok}>✓ confirmed</span> : <span className="tag">draft</span>}
                      <span className={s.memSources} style={{ marginLeft: "auto" }}>from {c.sources.slice(0, 3).join(", ")}{c.sources.length > 3 ? ` +${c.sources.length - 3}` : ""}</span>
                    </div>
                    <div className={s.field} style={{ marginBottom: 8 }}>
                      <textarea value={c.draft} onChange={(e) => setCards((cs) => cs.map((x, j) => (j === i ? { ...x, draft: e.target.value } : x)))} />
                    </div>
                    <button
                      className="btn btn-primary"
                      disabled={busy}
                      onClick={() =>
                        guard(async () => {
                          await apiPost("/onboarding/confirm", { slice: c.slice, content: c.draft });
                          await refresh();
                        })
                      }
                    >
                      {confirmed.includes(c.slice) ? "Save again" : "Confirm"}
                    </button>
                  </div>
                ))}
              </div>
              <div className={s.actions}>
                <button className="btn" disabled={!status?.steps.cards} onClick={() => guard(async () => {
                  const [ck, apr] = await Promise.all([apiGet<Cockpit>("/cockpit"), apiGet<Approval[]>("/approvals")]);
                  setCockpit(ck); setApprovals(apr); setStep(4);
                })}>First pulse →</button>
              </div>
            </div>
          </div>
        ) : null}

        {step === 4 ? (
          <div className="card">
            <div className="card-header"><div className="card-title">First pulse, first approval</div></div>
            <div className="card-body">
              {cockpit ? (
                <>
                  <div className="pulse-content">{cockpit.pulse.text}</div>
                  <div className={s.hint}>{cockpit.pulse.source === "run" ? `From the daemon (Tier ${cockpit.pulse.tier}).` : status?.first_pulse_ready ? "The first pulse landed — reloading…" : chainActive(status) ? "The first-pulse chain is still running in the daemon; this page refreshes itself." : "Tier-0 text: the daemon has not run cockpit.morning_pulse yet. Compile queues it."}</div>
                  <div className="cash-strip" style={{ marginTop: 12 }}>{cockpit.tiles.map((t) => <Tile key={t.label} tile={t} />)}</div>
                  {approvals.length === 0 ? <div className="muted small">No approvals waiting — the daemon proposes them from signals.</div> : approvals.slice(0, 2).map((a) => <ApprovalRow key={a.id} approval={a} />)}
                </>
              ) : (
                <button className="btn" onClick={() => guard(async () => { setCockpit(await apiGet<Cockpit>("/cockpit")); setApprovals(await apiGet<Approval[]>("/approvals")); })}>Load</button>
              )}
              <div className={s.actions}><button className="btn" onClick={() => setStep(5)}>Set the cadence →</button></div>
            </div>
          </div>
        ) : null}

        {step === 5 ? (
          <div className="card">
            <div className="card-header"><div className="card-title">Set the cadence</div></div>
            <div className="card-body">
              <div className={s.row}>
                <div className={s.field}><label className={s.label}>Timezone</label><input value={tz} onChange={(e) => setTz(e.target.value)} /></div>
                <div className={s.field}><label className={s.label}>Pulse hour (local)</label><input type="number" min={0} max={23} value={hour} onChange={(e) => setHour(parseInt(e.target.value || "7", 10))} /></div>
              </div>
              <div className={s.row}>
                <div className={s.field}><label className={s.label}>Where</label>
                  <select value={channel} onChange={(e) => setChannel(e.target.value as "web" | "slack" | "both")}><option value="web">Web</option><option value="slack">Slack</option><option value="both">Both</option></select>
                </div>
                <div className={s.field}><label className={s.label}>Monthly Tier-2 token budget</label><input type="number" value={budget} onChange={(e) => setBudget(parseInt(e.target.value || "0", 10))} /></div>
              </div>
              <div className={s.hint}>
                {budgetCeiling !== null
                  ? `Your plan allows up to ${budgetCeiling.toLocaleString()} Tier-2 tokens/month.`
                  : "Founder tier default: 1.5M Tier-2 tokens/month."}{" "}
                Overage degrades to Tier 0/1 — never a surprise invoice.
              </div>
              <div className={s.actions}>
                <button className="btn btn-primary" disabled={busy} onClick={() => guard(async () => { await apiPost("/onboarding/cadence", { timezone: tz, pulse_hour: hour, channel, tier2_tokens_allowed: budget }); await refresh(); })}>Save cadence</button>
                {status?.steps.cadence ? <Link className="btn" href="/cockpit">Open the Cockpit →</Link> : null}
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </Shell>
  );
}
