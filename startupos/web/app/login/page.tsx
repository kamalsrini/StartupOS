"use client";

// Sign-in. Sprint 3d (Track G).
//
// One public URL means this page has two very different visitors and must never confuse them:
//
//   1. someone with an account — one "Continue with Google" button;
//   2. someone Google vouches for who has no account anywhere — offered a NEW company of their own.
//
// (2) is the dangerous one. A person invited to a colleague's company who signs in with the wrong address
// would land here, and if the copy said "create your account" they would cheerfully create a second, empty
// company, see none of their colleague's work, and conclude the product is broken. So the sign-up panel says
// what it is creating, whose data will (not) be in it, and what to do instead if they were invited.

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ALLOW_BOOTSTRAP, ApiError, apiPost, apiUrl, me, providers, safeNext, type Providers } from "@/lib/api";

/** Copy for every `?error=` the API's Google callback can send us back with. */
const ERRORS: Record<string, string> = {
  cancelled: "Sign-in was cancelled. Nothing changed — you can try again.",
  state_mismatch:
    "That sign-in attempt expired or did not start here. Press “Continue with Google” to start a fresh one.",
  email_unverified:
    "Google has not verified that e-mail address, so we cannot use it to identify you. Verify it with Google, or sign in with a different Google account.",
  failed: "Google sign-in did not complete. Nothing changed — please try again.",
  account_inactive:
    "That address belongs to a company on StartupOS, but the account is not active. Ask that company’s owner to re-invite you.",
  signup_closed:
    "This installation is not accepting new companies right now, so none was created. If you were invited to a company that is already here, ask its owner to invite this exact address and then sign in.",
  not_configured:
    "Google sign-in is not configured on this installation. The operator needs to create a Google OAuth client (docs/GOOGLE-SIGNIN.md).",
};

function LoginInner() {
  const params = useSearchParams();
  // Same-origin paths only: this value ends up in window.location, and the query string is attacker-supplied.
  const next = safeNext(params.get("next"));
  // Set by the API's Google callback when a verified identity matched no user at all.
  const signUpForParam = params.get("new") === "google" ? params.get("email") : null;
  const errorParam = params.get("error");

  const [avail, setAvail] = useState<Providers>({ google: true, bootstrap: ALLOW_BOOTSTRAP, signup: true });
  // A stale `?new=google` link must not show a "create my company" form the API will refuse: when the operator
  // has closed sign-up, this page falls back to the plain sign-in panel, which says so.
  const signUpFor = avail.signup ? signUpForParam : null;
  const [company, setCompany] = useState("");
  const [website, setWebsite] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(errorParam ? (ERRORS[errorParam] ?? "Sign-in failed.") : null);

  useEffect(() => {
    // Already signed in → straight to the app. Not while finishing a sign-up: the identity is verified but
    // has no account yet, so /auth/me is 401 and there is nothing to jump to.
    if (signUpFor) return;
    me()
      .then((m) => {
        if (m) window.location.replace(next);
      })
      .catch(() => undefined);
  }, [next, signUpFor]);

  useEffect(() => {
    providers().then(setAvail).catch(() => undefined);
  }, []);

  async function bootstrap() {
    setBusy(true);
    setErr(null);
    try {
      await apiPost("/auth/bootstrap", { token, email, name: name || undefined });
      window.location.assign(next);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  /** Create a brand-new company for the Google identity the API just verified. */
  async function createCompany() {
    setBusy(true);
    setErr(null);
    try {
      // No id_token in the browser: the API reads the signed, HttpOnly `sos_signup` cookie its callback set.
      await apiPost("/onboarding/tenant", { name: company.trim(), website: website.trim() || undefined });
      window.location.assign("/onboarding");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const googleButton = (label: string) =>
    avail.google ? (
      <a className="btn btn-primary" href={apiUrl("/auth/google")} style={{ textAlign: "center", display: "block" }}>
        {label}
      </a>
    ) : (
      <div className="small muted">
        Google sign-in is not configured on this installation. The operator has to create a Google OAuth client
        (see <code>docs/GOOGLE-SIGNIN.md</code>) before anyone can sign in this way.
      </div>
    );

  return (
    <div className="app" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
      <div className="card" style={{ width: 420, maxWidth: "94vw" }}>
        <div className="card-header">
          <div className="brand-mark">S</div>
          <div className="card-title">{signUpFor ? "Start your own company" : "Sign in to StartupOS"}</div>
        </div>
        <div className="card-body" style={{ display: "grid", gap: 12 }}>
          {err ? <div className="error-box" style={{ marginBottom: 0 }}>{err}</div> : null}

          {signUpFor ? (
            <>
              <div style={{ fontSize: 13, lineHeight: 1.6 }}>
                Google signed you in as <b>{signUpFor}</b>. No StartupOS account uses that address yet, so we can
                set you up with <b>a new company of your own</b>.
              </div>
              <div className="small muted" style={{ lineHeight: 1.6 }}>
                This creates a <b>brand-new, empty workspace</b> for you alone. It is <b>not</b> how you join a
                company that is already on StartupOS — you will not see a colleague’s work, cash or customers in
                it, and they will not see yours.
                <br />
                <br />
                <b>Were you invited to a colleague’s company?</b> Do not create a company here. Ask them to invite{" "}
                <b>{signUpFor}</b> (this exact address), then come back and sign in — companies are invite-only.
              </div>
              <div style={{ display: "grid", gap: 8 }}>
                <label className="small muted" htmlFor="company">
                  Your company’s name
                </label>
                <input
                  id="company"
                  placeholder="UnitOne"
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                />
                <label className="small muted" htmlFor="website">
                  Website (optional)
                </label>
                <input id="website" placeholder="unitone.ai" value={website} onChange={(e) => setWebsite(e.target.value)} />
                <button className="btn btn-primary" disabled={busy || !company.trim()} onClick={createCompany}>
                  {busy ? "Creating…" : "Create my company →"}
                </button>
                <div className="small muted">
                  You will be its owner, and the next step connects your tools. Signed in as the wrong Google
                  account? <a href={apiUrl("/auth/google")}>Use a different account</a>.
                </div>
              </div>
            </>
          ) : (
            <>
              {googleButton("Continue with Google")}
              {/* Sprint 3d PE review: the operator can close self-serve sign-up (STARTUPOS_ALLOW_SIGNUP=0). The
                  page has to say so HERE — a stranger who is told nothing walks through a whole Google round
                  trip to be refused at the end, which is the dead end this page exists to remove. */}
              <div className="small muted" style={{ lineHeight: 1.6 }}>
                Already part of a company on StartupOS? Use the Google account it invited — companies are
                invite-only, so the address has to match the invitation exactly.
                <br />
                <br />
                {avail.signup ? (
                  <>
                    Nobody invited you? Continuing with Google sets up a <b>new company of your own</b>, separate
                    from everyone else’s.
                  </>
                ) : (
                  <>
                    <b>New companies are closed on this installation.</b> Signing in works only for an address
                    that already belongs to a company here — nothing is created for anyone else. If you were
                    invited, ask that company’s owner to invite your exact address; otherwise ask whoever runs
                    this installation.
                  </>
                )}
              </div>
            </>
          )}

          {avail.bootstrap && ALLOW_BOOTSTRAP && !signUpFor ? (
            <details>
              <summary className="small muted">Bootstrap owner (single-operator install)</summary>
              <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
                <input placeholder="Bootstrap token" type="password" value={token} onChange={(e) => setToken(e.target.value)} />
                <input placeholder="you@company.com" value={email} onChange={(e) => setEmail(e.target.value)} />
                <input placeholder="Your name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
                <button className="btn" disabled={busy || !token || !email} onClick={bootstrap}>
                  Create owner &amp; sign in
                </button>
              </div>
            </details>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginInner />
    </Suspense>
  );
}
