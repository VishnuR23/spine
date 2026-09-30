import { useEffect, useState } from "react";
import { Link } from "react-router";
import { spineFetch } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveActivity } from "@/contexts/LiveActivityContext";
import {
  Alert,
  Badge,
  Card,
  CardHeader,
  decisionBadgeVariant,
  EmptyState,
  LiveIndicator,
  MetricCard,
  PageHeader,
  Skeleton,
} from "@/components/ui";

interface Agent {
  id: string;
  name: string;
  is_active: boolean;
}

interface Policy {
  id: string;
  name: string;
  is_active: boolean;
}

interface AuditEvent {
  id: string;
  agent_id: string;
  action_type: string;
  target_resource: string;
  policy_decision: string;
  timestamp?: string;
}

type PlanSession = {
  id: string;
  agent_id: string;
  goal: string;
  status: string;
  drift_score: number;
  evaluation_count: number;
};

function driftTone(score: number): string {
  if (score >= 0.6) return "text-rose-300";
  if (score >= 0.3) return "text-amber-300";
  return "text-emerald-300";
}

export function HomePage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const { streamConnected, streamError, pendingCount, interceptFeed } =
    useLiveActivity();

  const [agents, setAgents] = useState<Agent[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [sessions, setSessions] = useState<PlanSession[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      spineFetch<Agent[]>("/v1/agents").catch(() => [] as Agent[]),
      spineFetch<Policy[]>("/v1/policies").catch(() => [] as Policy[]),
      spineFetch<AuditEvent[]>("/v1/audit?limit=12").catch(() => [] as AuditEvent[]),
      spineFetch<PlanSession[]>("/v1/sessions?status_filter=active&limit=50").catch(
        () => [] as PlanSession[],
      ),
    ]).then(([a, p, e, s]) => {
      setAgents(a);
      setPolicies(p);
      setEvents(e);
      setSessions(s);
      setLoading(false);
    });
  }, []);

  // Live-bump drift on SSE plan_evaluation events for any active session shown here
  // (we already get them via LiveActivityContext; the sessions list refreshes on
  // next nav. Keeping this surface tight rather than re-subscribing locally.)

  const liveEvents =
    interceptFeed.length > 0
      ? interceptFeed.map((e) => ({
          id: e.id,
          agent_id: e.agent_id,
          action_type: e.action_type,
          target_resource: e.target_resource ?? "—",
          policy_decision: e.decision,
          timestamp: e.ts,
        }))
      : events;

  const activeAgents = agents.filter((a) => a.is_active).length;
  const activePolicies = policies.filter((p) => p.is_active).length;
  const activeSessions = sessions.filter((s) => s.status === "active").length;
  const driftingSessions = sessions.filter(
    (s) => s.status === "active" && s.drift_score >= 0.3,
  ).length;

  const recentAllowed = liveEvents.filter((e) => e.policy_decision === "allowed").length;
  const recentBlocked = liveEvents.filter((e) => e.policy_decision === "blocked").length;

  return (
    <div className="space-y-8">
      <PageHeader
        title={user?.org_name || "Overview"}
        description="Real-time governance for autonomous AI agents — policy enforcement, plan-bound monitoring, and human approval all in one place."
        badge={<LiveIndicator live={streamConnected} />}
      />

      {streamError ? <Alert variant="warning">{streamError}</Alert> : null}

      {/* Top metric strip */}
      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : (
        <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Active sessions"
            value={activeSessions}
            hint={
              driftingSessions > 0
                ? `${driftingSessions} drifting`
                : activeSessions === 0
                  ? "No work in flight"
                  : "All within plan"
            }
            trend={driftingSessions > 0 ? "down" : "neutral"}
          />
          <MetricCard label="Active agents" value={activeAgents} />
          <MetricCard
            label="Pending approvals"
            value={pendingCount}
            hint={pendingCount > 0 ? "Awaiting review" : "Queue clear"}
            trend={pendingCount > 0 ? "down" : "neutral"}
          />
          <MetricCard
            label="Recent decisions"
            value={`${recentAllowed} / ${recentBlocked}`}
            hint="allowed / blocked"
          />
        </section>
      )}

      {/* Attention row: approvals + drifting sessions */}
      {(pendingCount > 0 || driftingSessions > 0) && isAdmin ? (
        <section className="grid gap-4 md:grid-cols-3">
          {pendingCount > 0 ? (
            <Card className="border-amber-500/30">
              <CardHeader
                title="Approvals awaiting review"
                subtitle={`${pendingCount} flagged action${pendingCount === 1 ? "" : "s"}`}
                actions={
                  <Link to="/approvals" className="text-xs font-medium text-spine-accent hover:text-white">
                    Open queue →
                  </Link>
                }
              />
              <p className="px-6 pb-4 text-sm text-spine-muted">
                Decide inline from the Audit log or open the approvals queue for the full
                review surface.
              </p>
            </Card>
          ) : null}
          {driftingSessions > 0 ? (
            <Card className="border-amber-500/30">
              <CardHeader
                title="Sessions drifting from plan"
                subtitle={`${driftingSessions} active session${driftingSessions === 1 ? "" : "s"} above 30%`}
                actions={
                  <Link to="/sessions" className="text-xs font-medium text-spine-accent hover:text-white">
                    Investigate →
                  </Link>
                }
              />
              <ul className="divide-y divide-spine-border px-2 pb-2">
                {sessions
                  .filter((s) => s.status === "active" && s.drift_score >= 0.3)
                  .slice(0, 3)
                  .map((s) => (
                    <li key={s.id} className="px-4 py-3 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <p className="truncate text-xs text-spine-fg">{s.goal}</p>
                        <span
                          className={`tabular-nums font-semibold text-xs ${driftTone(s.drift_score)}`}
                        >
                          {(s.drift_score * 100).toFixed(0)}%
                        </span>
                      </div>
                    </li>
                  ))}
              </ul>
            </Card>
          ) : null}
        </section>
      ) : null}

      {/* Live activity */}
      <Card>
        <CardHeader
          title="Live decision feed"
          subtitle={
            streamConnected
              ? "Updates as agents call tools"
              : "Connecting to the live activity stream…"
          }
          actions={
            <Link to="/audit" className="text-xs font-medium text-spine-accent hover:text-white">
              Audit log →
            </Link>
          }
        />
        {liveEvents.length === 0 ? (
          <EmptyState
            title="No decisions yet"
            description="Once an agent calls Spine, decisions stream here in real time. Connect Claude Code or any of the SDKs to get started."
            action={
              <Link
                to="/guide"
                className="inline-flex rounded-lg bg-white px-4 py-2 text-sm font-medium text-zinc-950"
              >
                View setup guide
              </Link>
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Decision</th>
                  <th>Action</th>
                  <th>Target</th>
                </tr>
              </thead>
              <tbody>
                {liveEvents.slice(0, 8).map((ev) => (
                  <tr key={ev.id}>
                    <td className="whitespace-nowrap text-xs text-spine-muted">
                      {ev.timestamp
                        ? new Date(ev.timestamp).toLocaleString(undefined, {
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                            second: "2-digit",
                          })
                        : "—"}
                    </td>
                    <td>
                      <Badge variant={decisionBadgeVariant(ev.policy_decision)}>
                        {ev.policy_decision}
                      </Badge>
                    </td>
                    <td className="font-mono text-xs text-spine-fg">{ev.action_type}</td>
                    <td className="max-w-[260px] truncate font-mono text-xs">
                      {ev.target_resource}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Footer config strip */}
      <section className="grid gap-4 md:grid-cols-3">
        <QuickLink
          to="/sessions"
          title="Plan-bound sessions"
          desc="Declare what each agent task should do; watch drift in real time."
        />
        <QuickLink
          to="/policies"
          title="Policies"
          desc={
            activePolicies === 0
              ? "Author allow, deny, and flag rules to govern what agents can do."
              : "Edit allow, deny, and flag rules. Default-deny: only matching allow-policies pass."
          }
        />
        <QuickLink
          to="/audit"
          title="Audit log"
          desc="Every decision is recorded in a tamper-evident hash-chained log you can verify on demand."
        />
      </section>
    </div>
  );
}

function QuickLink({ to, title, desc }: { to: string; title: string; desc: string }) {
  return (
    <Link
      to={to}
      className="group surface-panel block p-5 transition-all hover:border-spine-border-strong hover:shadow-panel"
    >
      <p className="text-sm font-medium text-spine-fg group-hover:text-spine-accent transition-colors">
        {title}
      </p>
      <p className="mt-1 text-sm leading-relaxed text-spine-muted">{desc}</p>
    </Link>
  );
}
