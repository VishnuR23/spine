import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { NavLink } from "react-router";
import { sessionConfig } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveActivityOptional } from "@/contexts/LiveActivityContext";
import { cx } from "@/components/ui";

interface NavItem {
  to: string;
  label: string;
  adminOnly?: boolean;
  platformOnly?: boolean;
  section?: string;
}

const nav: NavItem[] = [
  { to: "/dashboard", label: "Overview", section: "Home" },
  { to: "/intercept", label: "Intercept", section: "Operations" },
  { to: "/audit", label: "Audit log", section: "Operations" },
  { to: "/sessions", label: "Sessions", section: "Operations" },
  { to: "/approvals", label: "Approvals", section: "Operations", adminOnly: true },
  { to: "/agents", label: "Agents", section: "Configure" },
  { to: "/policies", label: "Policies", section: "Configure", adminOnly: true },
  { to: "/webhooks", label: "Webhooks", section: "Configure", adminOnly: true },
  { to: "/guide", label: "Getting started", section: "Resources" },
  { to: "/settings", label: "Settings", section: "Resources" },
  { to: "/orgs", label: "Organizations", section: "Platform", platformOnly: true },
];

function NavSection({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-6 last:mb-0">
      <p className="mb-2 px-3 text-2xs font-medium uppercase tracking-wider text-spine-subtle">
        {label}
      </p>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-spine-border-strong bg-spine-elevated">
        <div className="h-3 w-3 rounded-sm bg-gradient-to-br from-indigo-400 to-violet-500" />
        <div className="absolute inset-0 rounded-lg ring-1 ring-inset ring-white/10" />
      </div>
      <span className="text-sm font-semibold tracking-tight text-spine-fg">Spine</span>
    </div>
  );
}

export function AppShell({
  children,
  onLogout,
}: {
  children: ReactNode;
  onLogout: () => void;
}) {
  const { user } = useAuth();
  const live = useLiveActivityOptional();
  const [platformAdmin, setPlatformAdmin] = useState(false);

  useEffect(() => {
    if (!user) {
      setPlatformAdmin(false);
      return;
    }
    sessionConfig()
      .then((c) => setPlatformAdmin(Boolean(c.platformAdmin)))
      .catch(() => setPlatformAdmin(false));
  }, [user]);

  const visibleNav = nav.filter((item) => {
    if (item.platformOnly) return platformAdmin;
    if (item.adminOnly) return user?.role === "admin";
    return true;
  });

  const sections = [...new Set(visibleNav.map((n) => n.section ?? "Other"))];

  return (
    <div className="flex min-h-screen bg-spine-bg">
      {/* Sidebar */}
      <aside className="fixed inset-y-0 left-0 z-20 hidden w-[240px] flex-col border-r border-spine-border bg-spine-surface/80 backdrop-blur-xl lg:flex">
        <div className="flex h-14 items-center border-b border-spine-border px-5">
          <Logo />
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-5">
          {sections.map((section) => (
            <NavSection key={section} label={section}>
              {visibleNav
                .filter((item) => (item.section ?? "Other") === section)
                .map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.to === "/dashboard"}
                    className={({ isActive }) =>
                      cx(
                        "flex items-center rounded-lg px-3 py-2 text-sm transition-colors",
                        isActive
                          ? "bg-spine-hover font-medium text-spine-fg shadow-soft"
                          : "text-spine-muted hover:bg-spine-hover/60 hover:text-spine-fg",
                      )
                    }
                  >
                    <span className="flex items-center gap-2">
                      {item.label}
                      {item.to === "/approvals" && live && live.pendingCount > 0 ? (
                        <span className="rounded-full bg-amber-500/20 px-1.5 py-0.5 text-2xs font-medium text-amber-200">
                          {live.pendingCount}
                        </span>
                      ) : null}
                    </span>
                  </NavLink>
                ))}
            </NavSection>
          ))}
        </nav>

        <div className="border-t border-spine-border p-4">
          {user ? (
            <div className="rounded-lg border border-spine-border bg-spine-elevated/50 p-3">
              <p className="truncate text-sm font-medium text-spine-fg">{user.name}</p>
              <p className="truncate text-2xs text-spine-subtle">{user.email}</p>
              {user.org_name ? (
                <p className="mt-1 truncate text-2xs text-spine-muted">{user.org_name}</p>
              ) : null}
              <button
                type="button"
                onClick={onLogout}
                className="mt-3 w-full rounded-md border border-spine-border px-3 py-1.5 text-xs font-medium text-spine-muted transition-colors hover:border-spine-border-strong hover:bg-spine-hover hover:text-spine-fg"
              >
                Sign out
              </button>
            </div>
          ) : null}
        </div>
      </aside>

      {/* Mobile header + nav */}
      <header className="fixed inset-x-0 top-0 z-20 border-b border-spine-border bg-spine-surface/95 backdrop-blur-xl lg:hidden">
        <div className="flex h-14 items-center justify-between px-4">
          <Logo />
          <button
            type="button"
            onClick={onLogout}
            className="rounded-lg border border-spine-border px-3 py-1.5 text-xs text-spine-muted"
          >
            Sign out
          </button>
        </div>
        <nav className="flex gap-1 overflow-x-auto border-t border-spine-border px-3 py-2 scrollbar-none">
          {visibleNav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/dashboard"}
              className={({ isActive }) =>
                cx(
                  "shrink-0 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                  isActive ? "bg-spine-hover text-spine-fg" : "text-spine-muted",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col lg:pl-[240px]">
        <main className="flex-1 px-4 pb-12 pt-[7.5rem] lg:px-8 lg:pt-8">
          <div className="mx-auto max-w-6xl page-enter">{children}</div>
        </main>
        <footer className="border-t border-spine-border px-4 py-4 lg:px-8">
          <p className="mx-auto max-w-6xl text-2xs text-spine-subtle">
            Spine — policy, audit, and oversight for autonomous agents.
          </p>
        </footer>
      </div>
    </div>
  );
}
