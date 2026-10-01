import { FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError, spineFetch } from "@/api/client";
import { DeactivateResourceActions } from "@/components/DeactivateResource";
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
  PageHeader,
  Spinner,
  Textarea,
} from "@/components/ui";

type Policy = {
  id: string;
  org_id: string;
  agent_id: string | null;
  name: string;
  rule_type: string | null;
  rule_config: Record<string, unknown>;
  compliance_framework: string | null;
  is_active: boolean;
};

const defaultRule = `{
  "effect": "deny",
  "action_types": ["order.place", "order.place.large", "order.amend"],
  "target_resource_regex": "^(RSTR|ACME)$"
}`;

export function PoliciesPage() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("Restricted list: no orders in restricted names");
  const [ruleType, setRuleType] = useState("action");
  const [agentId, setAgentId] = useState("");
  const [ruleJson, setRuleJson] = useState(defaultRule);

  const load = useCallback(async () => {
    setError(null);
    try {
      const rows = await spineFetch<Policy[]>("/v1/policies", { method: "GET" });
      setPolicies(rows);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load policies");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    setError(null);
    let rule_config: Record<string, unknown>;
    try {
      rule_config = JSON.parse(ruleJson) as Record<string, unknown>;
    } catch {
      setError("rule_config must be valid JSON");
      return;
    }
    try {
      await spineFetch<Policy>("/v1/policies", {
        method: "POST",
        json: {
          name,
          rule_type: ruleType || null,
          agent_id: agentId.trim() ? agentId.trim() : null,
          rule_config,
        },
      });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Create failed");
    }
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Policies"
        description="Pre-trade rules: restricted lists, notional bands, trading hours, entitlements. Default-deny — an order no policy allows is refused. Deactivate a policy to stop enforcing it without deleting history."
      />

      <Card>
        <CardHeader title="Create policy" subtitle="Define effect, action types, and target patterns in rule_config." />
        <CardBody>
          <form onSubmit={onCreate} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} required />
              </Field>
              <Field label="Rule type">
                <Input value={ruleType} onChange={(e) => setRuleType(e.target.value)} />
              </Field>
            </div>
            <Field label="Agent ID" hint="Leave blank for org-wide policy">
              <Input
                value={agentId}
                onChange={(e) => setAgentId(e.target.value)}
                className="font-mono text-xs"
                placeholder="Optional UUID"
              />
            </Field>
            <Field label="rule_config (JSON)">
              <Textarea
                value={ruleJson}
                onChange={(e) => setRuleJson(e.target.value)}
                rows={10}
                className="font-mono text-xs"
              />
            </Field>
            <Button type="submit">Create policy</Button>
          </form>
        </CardBody>
      </Card>

      {error ? <Alert variant="error">{error}</Alert> : null}

      <Card>
        <CardHeader
          title="Policies"
          subtitle={`${policies.length} total · inactive policies are ignored by intercept`}
        />
        {loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-6 w-6" />
          </div>
        ) : policies.length === 0 ? (
          <EmptyState title="No policies" description="Create your first policy. Until one allows it, every order is refused." />
        ) : (
          <ul className="divide-y divide-spine-border">
            {policies.map((p) => (
              <li key={p.id} className="px-6 py-5">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-medium text-spine-fg">{p.name}</p>
                    <p className="mt-0.5 font-mono text-2xs text-spine-subtle">{p.id}</p>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
                    <Badge variant={p.is_active ? "success" : "muted"}>
                      {p.is_active ? "Active" : "Inactive"}
                    </Badge>
                    <DeactivateResourceActions
                      kind="policy"
                      id={p.id}
                      label={p.name}
                      isActive={p.is_active}
                      onChanged={load}
                      onError={setError}
                    />
                  </div>
                </div>
                <pre className="mt-3 max-h-36 overflow-auto rounded-lg border border-spine-border bg-spine-bg p-3 font-mono text-2xs text-spine-muted">
                  {JSON.stringify(p.rule_config, null, 2)}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
