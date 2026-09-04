"use client";

import { useEffect, useState } from "react";
import Sidebar from "./Sidebar";
import { getTenant } from "@/lib/api";

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
  const [tenant, setTenantState] = useState<string | null>(null);
  useEffect(() => setTenantState(getTenant()), []);
  return (
    <div className="app">
      <Sidebar tenant={tenant} />
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
