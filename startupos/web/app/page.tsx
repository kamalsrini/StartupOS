"use client";

// The front door. Sprint 3d (Track G): the deployed app is one public URL, so `/` is the link a founder sends
// to somebody — it has to decide, on its own, between "you are signed in" and "here is how you sign in".
//
// This used to be a next.config redirect straight to /cockpit, which meant a signed-out visitor loaded the
// cockpit, watched it fail, and only then arrived at /login. Now the only hop a stranger makes is to /login.

import { useEffect } from "react";
import { me } from "@/lib/api";

export default function Home() {
  useEffect(() => {
    me()
      .then((m) => window.location.replace(m ? "/cockpit" : "/login"))
      .catch(() => window.location.replace("/login"));
  }, []);
  return (
    <main
      style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}
      aria-busy="true"
    >
      <div className="small muted">Loading StartupOS…</div>
    </main>
  );
}
