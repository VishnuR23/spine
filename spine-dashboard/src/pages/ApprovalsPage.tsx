import { useCallback, useEffect, useState } from "react";
import { ApiError, spineFetch } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveActivity } from "@/contexts/LiveActivityContext";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  LiveIndicator,
  PageHeader,
  Select,
  Spinner,
} from "@/components/ui";

type Approval = {
  id: string;
  org_id: string;
  agent_id: string;
  status: string;
  decision: string | null;
  decided_by: string | null;
  audit_event_id: string | null;
  proposed: Record<string, unknown>;
};

function proposedSummary(proposed: Record<string, unknown>) {
  const action = proposed.action as Record<string, unknown> | undefined;
  if (!action) return { action_type: "—", target: "—" };
  return {
    action_type: String(action.action_type ?? "—"),
    target: action.target_resource != null ? String(action.target_resource) : "—",
  };
}

export function ApprovalsPage() {
  const { user } = useAuth();
  const { streamConnected, decideApproval } = useLiveActivity();
  const [statusFilter, setStatusFilter] = useState("pending");
  const [rows, setRows] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);

  const orgId = user?.org_id ?? "";

  const load = useCallback(async () => {
    if (!orgId) return;
    setError(null);
    setLoading(true);
    try {
      const q = new URLSearchParams({ org_id: orgId });
      if (statusFilter.trim()) q.set("status_filter", statusFilter.trim());
      const list = await spineFetch<Approval[]>(`/v1/approvals?${q.toString()}`, { method: "GET" });
      setRows(list);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load approvals");
    } finally {
      setLoading(false);
    }
  }, [orgId, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onDecide(id: string, action: "approve" | "reject") {
    setActingId(id);
    setError(null);
    try {
      await decideApproval(id, action);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Decision failed");
    } finally {
      setActingId(null);
    }
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Approvals"
        description="Four-eyes sign-off on flagged orders. Allow grants a one-hour window for the agent to resend the same order."
        badge={<LiveIndicator live={streamConnected} />}
        actions={
          <Button variant="secondary" size="sm" onClick={() => void load()}>
            Refresh
          </Button>
        }
      />

      {error ? <Alert variant="error">{error}</Alert> : null}

      <Card>
        <CardHeader
          title="Queue"
          actions={
            <Select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="w-36 text-xs"
            >
              <option value="">All</option>
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
            </Select>
          }
        />
        {loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-6 w-6" />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="No tickets" description="Nothing in this queue. Try changing the filter." />
        ) : (
          <ul className="divide-y divide-spine-border max-h-[640px] overflow-y-auto">
            {rows.map((r) => {
              const { action_type, target } = proposedSummary(r.proposed);
              const pending = r.status === "pending";
              return (
                <li key={r.id} className="px-6 py-4 hover:bg-spine-hover/30 transition-colors">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      <p className="text-sm font-medium text-spine-fg">
                        {action_type}{" "}
                        <span className="font-normal text-spine-muted">on</span>{" "}
                        <span className="font-mono text-xs">{target}</span>
                      </p>
                      <p className="font-mono text-2xs text-spine-subtle break-all">{r.id}</p>
                      {r.decided_by ? (
                        <p className="text-2xs text-spine-muted">
                          {r.status} by {r.decided_by}
                        </p>
                      ) : null}
                    </div>
                    <Badge
                      variant={
                        r.status === "pending"
                          ? "warning"
                          : r.status === "approved"
                            ? "success"
                            : "danger"
                      }
                    >
                      {r.status}
                    </Badge>
                  </div>
                  {pending ? (
                    <div className="mt-4 flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        disabled={actingId === r.id}
                        onClick={() => void onDecide(r.id, "approve")}
                      >
                        {actingId === r.id ? "…" : "Allow"}
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={actingId === r.id}
                        onClick={() => void onDecide(r.id, "reject")}
                      >
                        Block
                      </Button>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
