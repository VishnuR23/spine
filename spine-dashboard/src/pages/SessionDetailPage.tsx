/**
 * Per-session drill-in: header, drift line chart, plan summary, evaluations timeline.
 *
 * Live-appends new plan_evaluation SSE events for this session.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError, spineFetch } from "@/api/client";
import { DriftChart } from "@/components/DriftChart";
import { InlineApprovalActions } from "@/components/InlineApprovalActions";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  LiveIndicator,
  PageHeader,
  Spinner,
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

type PlanEvaluation = {
  id: string;
  session_id: string;
  audit_event_id: string;
  agent_id: string;
  org_id: string;
  alignment: string;
  confidence: number;
  reasoning: string;
  drift_contribution: number;
  drift_score_after: number;
  model_id: string;
  approval_id: string | null;
  created_at: string;
};

function alignmentVariant(a: string): "success" | "warning" | "danger" | "muted" {
  if (a === "aligned") return "success";
  if (a === "drifted") return "warning";
  if (a === "divergent") return "danger";
  return "muted";
}

function driftTone(score: number): string {
  if (score >= 0.6) return "text-rose-300";
  if (score >= 0.3) return "text-amber-300";
  return "text-emerald-300";
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();

  const [session, setSession] = useState<PlanSession | null>(null);
  const [evaluations, setEvaluations] = useState<PlanEvaluation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [ending, setEnding] = useState(false);
  const [expandedRowId, setExpandedRowId] = useState<string | null>(null);

  // Subscribe to SSE: bump drift score + append new evaluation rows live
  const { connected: streamLive } = useSpineActivityStream((msg) => {
    if (!sessionId) return;
    if (msg.type === "plan_evaluation") {
      const d = msg.data as Record<string, unknown>;
      if (String(d.session_id || "") !== sessionId) return;
      const newDrift = Number(d.drift_score);
      // Update header
      setSession((prev) =>
        prev
          ? {
              ...prev,
              drift_score: Number.isNaN(newDrift) ? prev.drift_score : newDrift,
              evaluation_count: prev.evaluation_count + 1,
            }
          : prev,
      );
      // Append to list if not already there
      const incoming: PlanEvaluation = {
        id: String(d.id || `live-${Date.now()}`),
        session_id: String(d.session_id),
        audit_event_id: String(d.audit_event_id || ""),
        agent_id: String(d.agent_id || ""),
        org_id: String(d.org_id || ""),
        alignment: String(d.alignment || ""),
        confidence: Number(d.confidence || 0),
        reasoning: String(d.reasoning || ""),
        drift_contribution: Number(d.drift_contribution || 0),
        drift_score_after: Number(d.drift_score || 0),
        model_id: "stream",
        approval_id: d.approval_id ? String(d.approval_id) : null,
        created_at: msg.ts,
      };
      setEvaluations((prev) => {
        if (prev.some((e) => e.id === incoming.id)) return prev;
        return [...prev, incoming];
      });
    }
  });

  const load = useCallback(async () => {
    if (!sessionId) return;
    setError(null);
    try {
      const [s, evs] = await Promise.all([
        spineFetch<PlanSession>(`/v1/sessions/${sessionId}`),
        spineFetch<PlanEvaluation[]>(`/v1/sessions/${sessionId}/evaluations?limit=500`),
      ]);
      setSession(s);
      setEvaluations(evs);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load session");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const onEnd = async (status: "completed" | "abandoned") => {
    if (!sessionId || !session) return;
    setEnding(true);
    try {
      await spineFetch(`/v1/sessions/${sessionId}/end`, {
        method: "POST",
        json: { status },
      });
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to end session");
    } finally {
      setEnding(false);
    }
  };

  const chartPoints = useMemo(
    () =>
      evaluations.map((e) => ({
        drift_score_after: e.drift_score_after,
        alignment: e.alignment,
      })),
    [evaluations],
  );

  if (loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="space-y-4">
        <Alert variant="error">{error}</Alert>
        <Button variant="secondary" onClick={() => navigate("/sessions")}>
          ← Back to sessions
        </Button>
      </div>
    );
  }
  if (!session) {
    return (
      <div className="space-y-4">
        <Alert variant="warning">Session not found.</Alert>
        <Button variant="secondary" onClick={() => navigate("/sessions")}>
          ← Back to sessions
        </Button>
      </div>
    );
  }

  const isActive = session.status === "active";

  return (
    <div className="space-y-8">
      <div>
        <Link to="/sessions" className="text-xs text-spine-muted hover:text-spine-fg">
          ← Sessions
        </Link>
      </div>

      <PageHeader
        title={session.goal}
        description={
          <span>
            Plan-bound session ·{" "}
            <span className="font-mono text-2xs">{session.id.slice(0, 8)}…</span> · agent{" "}
            <span className="font-mono text-2xs">{session.agent_id.slice(0, 8)}…</span>
          </span>
        }
        badge={streamLive ? <LiveIndicator live /> : null}
        actions={
          isActive ? (
            <>
              <Button variant="secondary" disabled={ending} onClick={() => onEnd("completed")}>
                {ending ? "Ending…" : "Mark completed"}
              </Button>
              <Button variant="danger" disabled={ending} onClick={() => onEnd("abandoned")}>
                Abandon
              </Button>
            </>
          ) : null
        }
      />

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
            Drift score
          </p>
          <p className={`mt-2 text-2xl font-semibold tabular-nums ${driftTone(session.drift_score)}`}>
            {(session.drift_score * 100).toFixed(1)}%
          </p>
          <p className="mt-1 text-2xs text-spine-subtle">
            {session.drift_score >= 0.6
              ? "above block threshold (0.60)"
              : session.drift_score >= 0.4
                ? "above flag threshold (0.40)"
                : "below flag threshold"}
          </p>
        </Card>
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
            Evaluations
          </p>
          <p className="mt-2 text-2xl font-semibold tabular-nums text-spine-fg">
            {session.evaluation_count}
          </p>
        </Card>
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">Status</p>
          <p className="mt-2">
            <Badge variant={isActive ? "accent" : "muted"}>{session.status}</Badge>
          </p>
          {session.ended_at ? (
            <p className="mt-1 text-2xs text-spine-subtle">ended {formatTime(session.ended_at)}</p>
          ) : (
            <p className="mt-1 text-2xs text-spine-subtle">started {formatTime(session.created_at)}</p>
          )}
        </Card>
        <Card padding>
          <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
            Gate behavior
          </p>
          <p className="mt-2 text-sm text-spine-fg leading-relaxed">
            {session.drift_score >= 0.6
              ? "All new intercepts in this session are hard-blocked."
              : "Drift is below the block threshold — intercepts proceed normally."}
          </p>
        </Card>
      </section>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Drift over time"
            subtitle="Each point is one reviewer verdict. Dotted lines mark the flag (0.40) and block (0.60) thresholds."
          />
          <CardBody>
            <DriftChart points={chartPoints} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Declared plan" />
          <CardBody className="space-y-4 text-sm">
            <div>
              <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
                Goal
              </p>
              <p className="mt-1 text-spine-fg">{session.goal}</p>
            </div>
            {session.constraints.length > 0 ? (
              <div>
                <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
                  Constraints
                </p>
                <ul className="mt-1 space-y-1 text-spine-muted">
                  {session.constraints.map((c, i) => (
                    <li key={i} className="text-xs">
                      · {c}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {session.expected_resources.length > 0 ? (
              <div>
                <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
                  Expected resources
                </p>
                <ul className="mt-1 space-y-1 font-mono text-xs text-spine-muted">
                  {session.expected_resources.map((r, i) => (
                    <li key={i}>· {r}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {session.success_criteria ? (
              <div>
                <p className="text-2xs font-medium uppercase tracking-wider text-spine-subtle">
                  Success criteria
                </p>
                <p className="mt-1 text-xs text-spine-muted">{session.success_criteria}</p>
              </div>
            ) : null}
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Evaluations timeline"
          subtitle="Reviewer verdict for each order in the session. Click a row to read the reasoning."
        />
        {evaluations.length === 0 ? (
          <EmptyState
            title="No evaluations yet"
            description="When the agent sends an order in this session, the reviewer's verdict appears here."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Time</th>
                  <th>Alignment</th>
                  <th>Δ drift</th>
                  <th>Drift after</th>
                  <th>Confidence</th>
                  <th>Approval</th>
                </tr>
              </thead>
              <tbody>
                {evaluations.map((e, i) => {
                  const expanded = expandedRowId === e.id;
                  return (
                    <>
                      <tr
                        key={e.id}
                        className="cursor-pointer hover:bg-spine-hover/40"
                        onClick={() => setExpandedRowId(expanded ? null : e.id)}
                      >
                        <td className="tabular-nums text-spine-muted">{i + 1}</td>
                        <td className="whitespace-nowrap text-2xs text-spine-muted">
                          {formatTime(e.created_at)}
                        </td>
                        <td>
                          <Badge variant={alignmentVariant(e.alignment)}>{e.alignment}</Badge>
                        </td>
                        <td className="tabular-nums text-xs">
                          +{e.drift_contribution.toFixed(3)}
                        </td>
                        <td
                          className={`tabular-nums font-semibold ${driftTone(e.drift_score_after)}`}
                        >
                          {(e.drift_score_after * 100).toFixed(1)}%
                        </td>
                        <td className="tabular-nums text-xs">
                          {(e.confidence * 100).toFixed(0)}%
                        </td>
                        <td onClick={(ev) => ev.stopPropagation()}>
                          {e.approval_id ? (
                            <InlineApprovalActions
                              approvalId={e.approval_id}
                              compact
                              onDecided={(status) => {
                                // Clear the approval_id locally so buttons disappear
                                setEvaluations((prev) =>
                                  prev.map((row) =>
                                    row.id === e.id
                                      ? { ...row, approval_id: null }
                                      : row,
                                  ),
                                );
                                // status is recorded server-side; we just hide buttons
                                void status;
                              }}
                            />
                          ) : (
                            <span className="text-2xs text-spine-subtle">—</span>
                          )}
                        </td>
                      </tr>
                      {expanded ? (
                        <tr key={`${e.id}-reasoning`}>
                          <td colSpan={7} className="bg-spine-elevated/40">
                            <div className="px-6 py-4 text-xs leading-relaxed text-spine-fg">
                              <p className="font-medium text-spine-muted mb-1">Reviewer reasoning</p>
                              <p className="whitespace-pre-wrap">{e.reasoning}</p>
                              <p className="mt-3 text-2xs text-spine-subtle">
                                audit event{" "}
                                <span className="font-mono">
                                  {e.audit_event_id.slice(0, 8)}…
                                </span>
                                {e.model_id ? ` · model ${e.model_id}` : ""}
                              </p>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
