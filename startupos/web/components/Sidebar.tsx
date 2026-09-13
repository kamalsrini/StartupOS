"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { logout, type Me } from "@/lib/api";

const MODULES: [string, string, boolean?][] = [
  ["sales", "Sales"],
  ["marketing", "Marketing"],
  ["build", "Build"],
  ["finance", "Finance"],
  ["commerce", "Commerce", true],
  ["customers", "Customers"],
  ["research", "Research", true],
  ["social", "Social"],
  ["web", "Web"],
  ["security", "IT & Security"],
];

export default function Sidebar({ me }: { me: Me | null }) {
  const path = usePathname();
  async function signOut() {
    await logout();
    window.location.assign("/login");
  }
  const item = (href: string, label: string, stub?: boolean) => (
    <Link key={href} href={href} className={`nav-item${path === href ? " active" : ""}${stub ? " stub" : ""}`}>
      <span className="nav-dot" /> {label}
    </Link>
  );
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">S</div>
        <div className="brand-name">StartupOS</div>
      </div>
      <div className="nav-group-label">Workspace</div>
      {item("/cockpit", "Cockpit")}
      {item("/onboarding", "Onboarding")}
      {item("/settings", "Settings")}
      <div className="nav-group-label">Modules</div>
      {MODULES.map(([name, label, stub]) => item(`/m/${name}`, label, stub))}
      <div className="sidebar-foot">
        {me ? (
          <>
            <div title={me.user.id}>{me.user.email}</div>
            <div style={{ marginTop: 4 }}>Tenant: {me.user.tenant_id}</div>
            <button className="btn btn-ghost" style={{ marginTop: 8, padding: "2px 8px" }} onClick={signOut}>
              Logout
            </button>
          </>
        ) : (
          <div>Not signed in</div>
        )}
        <div style={{ marginTop: 4 }}>Memory: Postgres via API</div>
      </div>
    </aside>
  );
}
