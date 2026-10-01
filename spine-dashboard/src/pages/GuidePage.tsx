import type { ReactNode } from "react";
import { Link } from "react-router";
import { useAuth } from "@/contexts/AuthContext";
import { Card, CardBody, PageHeader } from "@/components/ui";

export function GuidePage() {
  const { user } = useAuth();

  return (
    <div className="space-y-8">
      <PageHeader
        title="Getting started"
        description="Put a trading agent behind pre-trade controls in under five minutes. Spine checks every order, amend, and cancel against your policies, reviews the agent against its mandate, and keeps a record you can prove."
      />

      <div className="space-y-6">
        <Step
          number={1}
          title="Mint an organization API key"
          content={
            <>
              An organization admin issues keys under{" "}
              <Link to="/settings" className="text-spine-accent hover:underline">
                Settings → API keys
              </Link>
              . The <code className="code-inline">spine_…</code> key is shown once; store
              it in your secrets manager. Agents send it as the{" "}
              <code className="code-inline">X-Org-Key</code> request header.
            </>
          }
        />
        <Step
          number={2}
          title="Register an agent identity"
          content={
            <>
              Each agent runtime gets its own identity under{" "}
              <Link to="/agents" className="text-spine-accent hover:underline">
                Agents
              </Link>
              . The returned UUID is what your audit log, plan evaluations, and approval
              tickets are scoped against.
            </>
          }
        />
        <Step
          number={3}
          title="Set your pre-trade policies"
          content={
            <>
              Spine is <strong className="text-spine-fg">default-deny</strong>: an order no
              policy allows is refused. On{" "}
              <Link to="/policies" className="text-spine-accent hover:underline">
                Policies
              </Link>
              , each rule allows, denies, or flags a set of action types and symbols —
              a restricted list is a deny on a symbol pattern, four-eyes review is a flag on{" "}
              <code className="code-inline">order.place.large</code>, trading hours are a
              time window. The finance pack in{" "}
              <code className="code-inline">examples/finance</code> seeds a full desk.
            </>
          }
        />
        <Step
          number={4}
          title="Put the gate on the order path"
          content={
            <>
              Link the C++ client in{" "}
              <code className="code-inline">sdks/cpp</code> into your order management or
              execution stack and call{" "}
              <code className="code-inline">OrderGate::check</code> before each order is
              sent. It sizes the order, asks Spine within a hard latency budget, and refuses
              the order if Spine cannot answer. For Python or TypeScript agents, use the SDKs
              in <code className="code-inline">sdks/</code>; for Claude Code, run{" "}
              <code className="code-inline">integrations/claude-code-spine/install.sh</code>.
            </>
          }
        />
        <Step
          number={5}
          title="Declare the trading mandate"
          content={
            <>
              Before it trades, the agent opens a session with its mandate: the goal,
              constraints, and the names it expects to trade. A context-isolated reviewer
              scores every order against that mandate and accumulates a drift score; a
              session that drifts past your threshold is blocked automatically. Sessions are
              visible on the{" "}
              <Link to="/sessions" className="text-spine-accent hover:underline">
                Sessions
              </Link>{" "}
              page.
            </>
          }
        />
        <Step
          number={6}
          title="Review and audit"
          content={
            <>
              Every decision lands in the{" "}
              <Link to="/audit" className="text-spine-accent hover:underline">
                Audit log
              </Link>{" "}
              as a tamper-evident chain. Orders waiting for four-eyes sign-off appear in{" "}
              <Link to="/approvals" className="text-spine-accent hover:underline">
                Approvals
              </Link>{" "}
              with inline allow/block buttons. Plan-bound drift and reviewer verdicts
              show on the{" "}
              <Link to="/sessions" className="text-spine-accent hover:underline">
                Sessions
              </Link>{" "}
              page.
            </>
          }
        />
      </div>

      <Card>
        <CardBody>
          <h2 className="text-sm font-medium text-spine-fg">Sample intercept request</h2>
          <pre className="mt-4 overflow-x-auto rounded-lg border border-spine-border bg-spine-bg p-4 font-mono text-xs leading-relaxed text-spine-muted">
{`curl -X POST https://api.your-deployment.example.com/v1/intercept \\
  -H "X-Org-Key: YOUR_ORG_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "agent_id": "AGENT_UUID",
    "session_id": "SESSION_UUID",
    "action": {
      "action_type": "order.place",
      "target_resource": "AAPL",
      "metadata": {
        "side": "BUY", "quantity": "100",
        "limit_price": "150.00", "notional": "15000.00"
      }
    }
  }'`}
          </pre>
        </CardBody>
      </Card>

      {user?.org_id ? (
        <Card>
          <CardBody>
            <h2 className="text-sm font-medium text-spine-fg">Your workspace</h2>
            <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-2xs uppercase tracking-wider text-spine-subtle">
                  Organization
                </dt>
                <dd className="mt-0.5 text-spine-fg">{user.org_name}</dd>
              </div>
              <div>
                <dt className="text-2xs uppercase tracking-wider text-spine-subtle">
                  Role
                </dt>
                <dd className="mt-0.5 capitalize text-spine-fg">{user.role}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-2xs uppercase tracking-wider text-spine-subtle">
                  Organization ID
                </dt>
                <dd className="mt-0.5 font-mono text-xs text-spine-muted">{user.org_id}</dd>
              </div>
            </dl>
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}

function Step({
  number,
  title,
  content,
}: {
  number: number;
  title: string;
  content: ReactNode;
}) {
  return (
    <div className="flex gap-4">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-spine-border bg-spine-elevated text-sm font-semibold text-spine-fg">
        {number}
      </div>
      <div className="pt-0.5">
        <h3 className="text-sm font-medium text-spine-fg">{title}</h3>
        <p className="mt-1.5 text-sm leading-relaxed text-spine-muted">{content}</p>
      </div>
    </div>
  );
}
