"use client";

import { useCallback, useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { API_BASE, apiGet, apiPost } from "@/lib/api";

/** GET /slack/status — never carries a token; `slack_installations` holds none. */
type SlackStatus = {
  configured: boolean;
  connected: boolean;
  team_id: string | null;
  team_name: string | null;
  /** The channel picked while installing — the fallback when `slack_channel` is unset. */
  default_channel: string | null;
  /** The founder's override, or null for "wherever the install put us". */
  slack_channel: string | null;
  /** What delivery resolves right now: the override, else the install's channel. */
  channel: string | null;
  pulse_channel: "web" | "slack" | "both" | string;
  delivers: boolean;
  installed_at: string | null;
  revoked_at: string | null;
  scopes: string[];
};

const OK_BOX: React.CSSProperties = {
  background: "#f0fdf4",
  border: "1px solid #bbf7d0",
  color: "#15803d",
  padding: "10px 12px",
  borderRadius: 6,
  fontSize: 12,
  marginBottom: 16,
};

const REASONS: Record<string, string> = {
  denied: "You cancelled the install in Slack. Nothing changed.",
  state: "That install link expired (they are good for 10 minutes). Start again.",
  exchange: "Slack refused the install. Check the app's Client ID and Secret with your operator.",
  workspace_taken: "That Slack workspace is already connected to another StartupOS company. One workspace, one company.",
  secrets: "Secret storage is not configured on this install (STARTUPOS_MASTER_KEY). Ask your operator.",
  install: "The install could not be saved. Nothing was written — try again.",
};

export default function SettingsPage() {
  const [slack, setSlack] = useState<SlackStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  const [channel, setChannel] = useState("");
  const [saving, setSaving] = useState(false);
  const [channelMsg, setChannelMsg] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const s = await apiGet<SlackStatus>("/slack/status");
      setSlack(s);
      setChannel(s.slack_channel ?? "");
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  /** POST /onboarding/cadence with only `slack_channel`: every other cadence field is left as it is. */
  const saveChannel = useCallback(async () => {
    setSaving(true);
    setChannelMsg(null);
    try {
      await apiPost("/onboarding/cadence", { slack_channel: channel.trim() });
      await load();
      setChannelMsg({
        kind: "ok",
        text: channel.trim() ? `StartupOS will post in ${channel.trim()}.` : "Using the channel you chose in Slack.",
      });
    } catch (e) {
      setChannelMsg({ kind: "bad", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }, [channel, load]);

  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const state = q.get("slack");
    if (state === "connected") setBanner({ kind: "ok", text: "Slack connected. Your pulse and digest can post there now." });
    else if (state === "error")
      setBanner({ kind: "bad", text: REASONS[q.get("reason") ?? ""] ?? "The Slack install did not finish." });
    if (state) window.history.replaceState({}, "", window.location.pathname);
    load();
  }, [load]);

  return (
    <Shell
      title="Settings"
      crumb="Where StartupOS reaches you"
      right={
        <button className="btn btn-ghost" onClick={load}>
          ↻ Refresh
        </button>
      }
    >
      {banner ? (
        banner.kind === "ok" ? (
          <div style={OK_BOX}>{banner.text}</div>
        ) : (
          <div className="error-box">{banner.text}</div>
        )
      ) : null}
      {err ? <div className="error-box">API unreachable: {err}</div> : null}

      <section className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <div className="card-title">Slack</div>
        </div>
        <div className="card-body">
        <p className="muted" style={{ marginTop: 0 }}>
          Install the StartupOS app into your own workspace. The bot token is stored encrypted for your company
          alone — it is never shared with another company and never shown again.
        </p>

        {!slack ? (
          <div className="muted">Loading…</div>
        ) : !slack.configured ? (
          <div className="error-box">
            Slack is not configured on this StartupOS install. Your operator needs to set
            <code> STARTUPOS_SLACK_CLIENT_ID</code>, <code>STARTUPOS_SLACK_CLIENT_SECRET</code> and
            <code> STARTUPOS_SLACK_SIGNING_SECRET</code>.
          </div>
        ) : slack.connected ? (
          <>
            <div style={{ margin: "8px 0" }}>
              Connected to <strong>{slack.team_name ?? slack.team_id}</strong>
              {slack.channel ? (
                <>
                  {" "}· posting to <code>{slack.channel}</code>
                  {slack.slack_channel ? null : " (chosen when you installed)"}
                </>
              ) : (
                <> · no channel yet</>
              )}
            </div>
            {slack.pulse_channel === "web" ? (
              <div className="muted">
                Your cadence is set to the web only, so nothing is posted to Slack. Change it in onboarding →
                Set the cadence.
              </div>
            ) : null}
            <div className="muted">Installed {slack.installed_at ? new Date(slack.installed_at).toLocaleString() : "—"}</div>

            <div style={{ marginTop: 16 }}>
              <label className="muted" htmlFor="slack-channel">
                Where should StartupOS post?
              </label>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
                <input
                  id="slack-channel"
                  value={channel}
                  onChange={(e) => setChannel(e.target.value)}
                  placeholder={slack.default_channel ?? "#general"}
                  style={{ padding: "6px 8px", minWidth: 220 }}
                />
                <button className="btn" disabled={saving} onClick={saveChannel}>
                  {saving ? "Saving…" : "Save channel"}
                </button>
              </div>
              <p className="muted" style={{ marginTop: 6 }}>
                A channel name (<code>#ops</code>) or a channel id (<code>C0123ABCD</code>). Leave it empty to use
                the channel you picked while installing{slack.default_channel ? <> (<code>{slack.default_channel}</code>)</> : null}.
                Invite the bot to a private channel first — StartupOS can post in public channels on its own.
              </p>
              {channelMsg ? (
                channelMsg.kind === "ok" ? (
                  <div style={OK_BOX}>{channelMsg.text}</div>
                ) : (
                  <div className="error-box">{channelMsg.text}</div>
                )
              ) : null}
            </div>
            <a className="btn btn-ghost" style={{ marginTop: 12 }} href={`${API_BASE}/slack/install`}>
              Reinstall / change workspace
            </a>
            <p className="muted" style={{ marginTop: 12 }}>
              To disconnect, remove the StartupOS app in Slack (Apps → StartupOS → Configuration → Remove). We drop
              the token and disable the connection the moment Slack tells us.
            </p>
          </>
        ) : (
          <>
            <a className="btn" style={{ marginTop: 8 }} href={`${API_BASE}/slack/install`}>
              Connect Slack
            </a>
            {slack.revoked_at ? (
              <div className="muted" style={{ marginTop: 8 }}>
                Previously connected to {slack.team_name ?? slack.team_id}; the app was removed in Slack.
              </div>
            ) : null}
          </>
        )}

        {slack ? (
          <details style={{ marginTop: 16 }}>
            <summary className="muted">What it asks for</summary>
            <ul className="muted">
              {slack.scopes.map((s) => (
                <li key={s}>
                  <code>{s}</code>
                </li>
              ))}
            </ul>
            <p className="muted">
              Read access is limited to the messages you send the bot. StartupOS never sends a message you did not
              approve.
            </p>
          </details>
        ) : null}
        </div>
      </section>
    </Shell>
  );
}
