"use client";

import { useEffect, useState } from "react";
import Sidebar from "./Sidebar";
import { me, type Me } from "@/lib/api";

export default function Shell({
  title,
  crumb,
  right,
  children,
}: {
  title: string;
  crumb?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [who, setWho] = useState<Me | null>(null);
  useEffect(() => {
    me()
      .then((m) => {
        if (!m) window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
        else setWho(m);
      })
      .catch(() => setWho(null));
  }, []);
  return (
    <div className="app">
      <Sidebar me={who} />
      <main className="main">
        <div className="topbar">
          <div className="topbar-title">{title}</div>
          {crumb ? <div className="topbar-crumb">{crumb}</div> : null}
          <div className="topbar-spacer" />
          {right}
        </div>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}
