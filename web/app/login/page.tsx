"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ALLOW_BOOTSTRAP, API_BASE, ApiError, apiPost, me } from "@/lib/api";

function LoginInner() {
  const params = useSearchParams();
  const next = params.get("next") || "/cockpit";
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    // Already signed in → straight to the app.
    me()
      .then((m) => {
        if (m) window.location.replace(next);
      })
      .catch(() => undefined);
  }, [next]);

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

  return (
    <div className="app" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
      <div className="card" style={{ width: 380 }}>
        <div className="card-header">
          <div className="brand-mark">S</div>
          <div className="card-title">Sign in to StartupOS</div>
        </div>
        <div className="card-body" style={{ display: "grid", gap: 12 }}>
          <a className="btn btn-primary" href={`${API_BASE}/auth/google`} style={{ textAlign: "center", display: "block" }}>
            Sign in with Google
          </a>
          <div className="small muted">
            Use the Google account your company owner invited. No account yet? Ask your owner to invite you.
          </div>
          {err ? <div className="error-box">{err}</div> : null}
          {ALLOW_BOOTSTRAP ? (
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
