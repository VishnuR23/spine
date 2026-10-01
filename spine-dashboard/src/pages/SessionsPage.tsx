/**
 * Sessions list + creation form.
 *
 * Filters by agent + status. Live-updates drift_score from SSE plan_evaluation
 * events. Row click → /sessions/:id detail.
 */
import { FormEvent, useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, spineFetch } from "@/api/client";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Field,
  Input,
  LiveIndicator,
  PageHeader,
  Select,
  Spinner,
  Textarea,
} from "@/components/ui";
import { useSpineActivityStream } from "@/hooks/useSpineActivityStream";

type PlanSession = {
  id: string;
  org_id: string;
  agent_id: string;
  goal: string;
  constraints: string[];
  expected_resources: string[];
  success_criteria: string | null;
  status: string;
  drift_score: number;
  evaluation_count: number;
  created_at: string;
  ended_at: string | null;
};

type Agent = {
  id: string;
  name: string;
  is_active: boolean;
};

function driftTone(score: number): string {
  if (score >= 0.6) return "text-rose-300";
  if (score >= 0.3) return "text-amber-300";
  return "text-emerald-300";
}

function statusVariant(s: string): "accent" | "muted" | "default" {
  if (s === "active") return "accent";
  if (s === "completed") return "muted";
  return "default";
}

function relativeTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

function DriftBar({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(1, score)) * 100;
  const color =
    score >= 0.6 ? "bg-rose-500/70" : score >= 0.3 ? "bg-amber-500/70" : "bg-emerald-500/70";
  return (
    <div className="flex items-center gap-2">
      <span className={`tabular-nums font-semibold ${driftTone(score)}`}>{pct.toFixed(0)}%</span>
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-spine-elevated">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

const STATUS_OPTIONS = ["", "active", "completed", "abandoned"] as const;

export function SessionsPage() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<PlanSession[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [agentFilter, setAgentFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  // Create-session form state
  const [showForm, setShowForm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createAgentId, setCreateAgentId] = useState("");
  const [createGoal, setCreateGoal] = useState("");
  const [createConstraints, setCreateConstraints] = useState("");
  const [createExpected, setCreateExpected] = useState("");
  const [createSuccess, setCreateSuccess] = useState("");

  // Live SSE: bump matching session's drift_score + evaluation_count
  const { connected: streamLive } = useSpineActivityStream((msg) => {
    if (msg.type !== "plan_evaluation") return;
    const d = msg.data as Record<string, unknown>;
    const sid = String(d.session_id || "");
    const newDrift = Number(d.drift_score);
    if (!sid || Number.isNaN(newDrift)) return;
    setSessions((prev) =>
      prev.map((s) =>
        s.id === sid
          ? { ...s, drift_score: newDrift, evaluation_count: s.evaluation_count + 1 }
          : s,
      ),
    );
  });

  const load = useCallback(async () => {
    setError(null);
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (agentFilter.trim()) params.set("agent_id", agentFilter.trim());
      if (statusFilter) params.set("status_filter", statusFilter);
      const [s, a] = await Promise.all([
        spineFetch<PlanSession[]>(`/v1/sessions?${params.toString()}`),
        spineFetch<Agent[]>("/v1/agents").catch(() => [] as Agent[]),
      ]);
      setSessions(s);
      setAgents(a);
      if (!createAgentId && a.length > 0) setCreateAgentId(a[0].id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load sessions");
    } finally {
      setLoading(false);
    }
  }, [agentFilter, statusFilter, createAgentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const submitCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreateError(null);
    setSubmitting(true);
    try {
      const body = {
        agent_id: createAgentId,
        goal: createGoal.trim(),
        constraints: createConstraints
          .split("\n")
          .map((l) => l.trim())
          .filter(Boolean),
        expected_resources: createExpected
          .split("\n")
          .map((l) => l.trim())
          .filter(Boolean),
        success_criteria: createSuccess.trim() || null,
      };
      const created = await spineFetch<PlanSession>("/v1/sessions", {
        method: "POST",
        json: body,
      });
      // Reset, hide form, jump to detail page
      setCreateGoal("");
      setCreateConstraints("");
      setCreateExpected("");
      setCreateSuccess("");
      setShowForm(false);
      navigate(`/sessions/${created.id}`);
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Failed to create session");
    } finally {
      setSubmitting(false);
    }
  };

  const activeCount = sessions.filter((s) => s.status === "active").length;
  const driftingCount = sessions.filter((s) => s.status === "active" && s.drift_score >= 0.3)
    .length;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Sessions"
        description="Trading mandates. Every order carrying a session_id is reviewed against the mandate the agent declared; drift that accumulates across orders blocks the session's further orders."
        badge={streamLive ? <LiveIndicator live /> : null}
        actions={
          <Button onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "New session"}
          </Button>
        }
      />

      {error ? <Alert variant="error">{error}</Alert> : null}

      {showForm ? (
        <Card>
          <CardHeader
            title="Declare a plan-bound session"
            subtitle="The reviewer compares every order against this mandate. Constraints and expected names are hints it reads alongside the goal."
          />
          <CardBody>
            {createError ? (
              <div className="mb-4">
                <Alert variant="error">{createError}</Alert>
              </div>
            ) : null}
            <form onSubmit={submitCreate} className="space-y-4">
              <Field label="Agent">
                <Select
                  value={createAgentId}
                  onChange={(e) => setCreateAgentId(e.target.value)}
                  required
                >
                  <option value="">— select agent —</option>
                  {agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} {a.is_active ? "" : "(inactive)"} · {a.id.slice(0, 8)}…
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Goal" hint="What this agent task is supposed to accomplish.">
                <Textarea
                  value={createGoal}
                  onChange={(e) => setCreateGoal(e.target.value)}
                  placeholder="Rebalance the US equity book toward the published target weights"
                  required
                  rows={3}
                />
              </Field>
              <Field
                label="Constraints"
                hint="One per line. e.g. 'only touch /src/auth/**', 'no schema changes'."
              >
                <Textarea
                  value={createConstraints}
                  onChange={(e) => setCreateConstraints(e.target.value)}
                  placeholder={"only trade names already held in the book\nno single order above 1M without approval\nno trading outside regular hours"}
                  rows={3}
                />
              </Field>
              <Field
                label="Expected resources"
                hint="Paths or URLs the agent is expected to access. One per line."
              >
                <Textarea
                  value={createExpected}
                  onChange={(e) => setCreateExpected(e.target.value)}
                  placeholder={"AAPL\nMSFT\nNVDA"}
                  rows={2}
                />
              </Field>
              <Field label="Success criteria" hint="Optional. How the reviewer should think about done.">
                <Input
                  value={createSuccess}
                  onChange={(e) => setCreateSuccess(e.target.value)}
                  placeholder="book weights within 50bps of target, no restricted names touched"
                />
              </Field>
              <div className="flex gap-2">
                <Button type="submit" disabled={submitting || !createAgentId || !createGoal.trim()}>
                  {submitting ? "Creating…" : "Create session"}
                </Button>
                <Button type="button" variant="ghost" onClick={() => setShowForm(false)}>
                  Cancel
                </Button>
              </div>
            </form>
          </CardBody>
        </Card>
      ) : null}

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">Active</p>
          <p className="mt-2 text-2xl font-semibold tabular-nums text-spine-fg">{activeCount}</p>
        </Card>
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
            Drifting (active)
          </p>
          <p className="mt-2 text-2xl font-semibold tabular-nums text-spine-fg">
            {driftingCount}
          </p>
          <p className="mt-1 text-2xs text-spine-subtle">drift ≥ 30%</p>
        </Card>
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">Total</p>
          <p className="mt-2 text-2xl font-semibold tabular-nums text-spine-fg">
            {sessions.length}
          </p>
        </Card>
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
            Stream
          </p>
          <p className="mt-2 text-sm text-spine-fg">
            {streamLive ? "Live updates on" : "Offline (Redis or SSE disabled)"}
          </p>
        </Card>
      </section>

      <Card>
        <CardHeader title="Filters" />
        <CardBody>
          <div className="flex flex-wrap items-end gap-4">
            <Field label="Agent ID" className="min-w-[220px] flex-1">
              <Input
                value={agentFilter}
                onChange={(e) => setAgentFilter(e.target.value)}
                placeholder="Filter by agent UUID"
                className="font-mono text-xs"
              />
            </Field>
            <Field label="Status" className="w-48">
              <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s || "all"} value={s}>
                    {s || "All statuses"}
                  </option>
                ))}
              </Select>
            </Field>
            <Button onClick={() => void load()}>Apply</Button>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Sessions"
          subtitle="Click a row to open the drift timeline and evaluations."
        />
        {loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-6 w-6" />
          </div>
        ) : sessions.length === 0 ? (
          <EmptyState
            title="No sessions yet"
            description="Declare a mandate above, or open a session from your trading agent before its first order."
            action={<Button onClick={() => setShowForm(true)}>New session</Button>}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Goal</th>
                  <th>Drift</th>
                  <th>Verdicts</th>
                  <th>Status</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr
                    key={s.id}
                    className="cursor-pointer hover:bg-spine-hover/40"
                    onClick={() => navigate(`/sessions/${s.id}`)}
                  >
                    <td className="font-mono text-xs text-spine-muted">{s.agent_id.slice(0, 8)}…</td>
                    <td className="max-w-[380px] truncate text-spine-fg text-xs leading-relaxed">
                      {s.goal}
                    </td>
                    <td>
                      <DriftBar score={s.drift_score} />
                    </td>
                    <td className="tabular-nums">{s.evaluation_count}</td>
                    <td>
                      <Badge variant={statusVariant(s.status)}>{s.status}</Badge>
                    </td>
                    <td className="whitespace-nowrap text-xs text-spine-muted">
                      {relativeTime(s.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-2xs text-spine-subtle">
        Tip: install the Claude Code hook (
        <Link to="/guide" className="text-spine-accent hover:text-white">
          guide
        </Link>
        ) and use{" "}
        <code className="rounded bg-spine-elevated px-1 py-0.5 font-mono text-2xs">
          /spine-session-start
        </code>{" "}
        to register a plan from a developer's machine.
      </p>
    </div>
  );
}
