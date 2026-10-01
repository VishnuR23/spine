import { FormEvent, useState } from "react";
import { ApiError, spineFetch } from "@/api/client";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  decisionBadgeVariant,
  Field,
  Input,
  PageHeader,
} from "@/components/ui";

type InterceptResponse = {
  allowed: boolean;
  decision: string;
  reason: string;
  audit_event_id: string;
  request_id: string;
  approval_id: string | null;
};

export function InterceptPage() {
  const [agentId, setAgentId] = useState("");
  const [actionType, setActionType] = useState("order.place");
  const [target, setTarget] = useState("AAPL");
  const [result, setResult] = useState<InterceptResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    setPending(true);
    try {
      const out = await spineFetch<InterceptResponse>("/v1/intercept", {
        method: "POST",
        json: {
          agent_id: agentId,
          action: {
            action_type: actionType,
            target_resource: target || null,
            metadata: { source: "spine-dashboard" },
          },
        },
      });
      setResult(out);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Intercept failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Intercept"
        description="Ask what Spine would answer for an order before an agent sends one. Uses your session credentials server-side, and records the decision like any other."
      />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Evaluate action"
            subtitle="Try order.place on a restricted symbol, or order.place.large to see four-eyes."
          />
          <CardBody>
            <form onSubmit={onSubmit} className="space-y-4">
              <Field label="Agent ID" hint="From the Agents page">
                <Input
                  value={agentId}
                  onChange={(e) => setAgentId(e.target.value)}
                  placeholder="00000000-0000-0000-0000-000000000000"
                  className="font-mono text-xs"
                  required
                />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Action type">
                  <Input value={actionType} onChange={(e) => setActionType(e.target.value)} required />
                </Field>
                <Field label="Target resource">
                  <Input
                    value={target}
                    onChange={(e) => setTarget(e.target.value)}
                    className="font-mono text-xs"
                  />
                </Field>
              </div>
              <Button type="submit" disabled={pending}>
                {pending ? "Evaluating…" : "Run intercept"}
              </Button>
            </form>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Result" subtitle={result ? "Policy decision" : "Run an intercept to see output"} />
          <CardBody>
            {error ? <Alert variant="error">{error}</Alert> : null}
            {!result && !error ? (
              <p className="text-sm text-spine-muted">
                Decisions include <span className="text-spine-fg">allowed</span>,{" "}
                <span className="text-spine-fg">blocked</span>, or{" "}
                <span className="text-spine-fg">flagged</span> for human review.
              </p>
            ) : null}
            {result ? (
              <dl className="space-y-4">
                <div>
                  <dt className="text-2xs uppercase tracking-wider text-spine-subtle">Decision</dt>
                  <dd className="mt-1 flex items-center gap-2">
                    <Badge variant={decisionBadgeVariant(result.decision)}>{result.decision}</Badge>
                    <span className="text-sm text-spine-muted">
                      {result.allowed ? "Action permitted" : "Action denied"}
                    </span>
                  </dd>
                </div>
                <div>
                  <dt className="text-2xs uppercase tracking-wider text-spine-subtle">Reason</dt>
                  <dd className="mt-1 text-sm text-spine-fg">{result.reason}</dd>
                </div>
                <div>
                  <dt className="text-2xs uppercase tracking-wider text-spine-subtle">Audit event</dt>
                  <dd className="mt-1 font-mono text-xs break-all text-spine-muted">{result.audit_event_id}</dd>
                </div>
                <div>
                  <dt className="text-2xs uppercase tracking-wider text-spine-subtle">Request ID</dt>
                  <dd className="mt-1 font-mono text-xs break-all text-spine-muted">{result.request_id}</dd>
                </div>
              </dl>
            ) : null}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
