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
} from "@/components/ui";

type Agent = {
  id: string;
  org_id: string;
  name: string;
  framework: string | null;
  is_active: boolean;
};

export function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [framework, setFramework] = useState("generic");

  const load = useCallback(async () => {
    setError(null);
    try {
      const rows = await spineFetch<Agent[]>("/v1/agents", { method: "GET" });
      setAgents(rows);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load agents");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onRegister(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await spineFetch<Agent>("/v1/agents/register", {
        method: "POST",
        json: { name: name || "agent", framework: framework || null },
      });
      setName("");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Register failed");
    }
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Agents"
        description="Register an identity for each trading agent. Every order check, approval, and audit row is scoped to one. Deactivate agents you no longer use."
      />

      <Card>
        <CardHeader title="Register agent" subtitle="Create a new agent for your organization." />
        <CardBody>
          <form onSubmit={onRegister} className="grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <Field label="Display name">
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="rebalancer"
                required
              />
            </Field>
            <Field label="Framework">
              <Input
                value={framework}
                onChange={(e) => setFramework(e.target.value)}
                placeholder="langchain, crewai, …"
              />
            </Field>
            <Button type="submit">Register</Button>
          </form>
        </CardBody>
      </Card>

      {error ? <Alert variant="error">{error}</Alert> : null}

      <Card>
        <CardHeader
          title="Registered agents"
          subtitle={`${agents.length} agent${agents.length === 1 ? "" : "s"}`}
        />
        {loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-6 w-6" />
          </div>
        ) : agents.length === 0 ? (
          <EmptyState title="No agents yet" description="Register your first agent before it sends any orders." />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Agent ID</th>
                  <th>Framework</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {agents.map((a) => (
                  <tr key={a.id}>
                    <td className="font-medium text-spine-fg">{a.name}</td>
                    <td className="font-mono text-xs">{a.id}</td>
                    <td>{a.framework ?? "—"}</td>
                    <td>
                      <Badge variant={a.is_active ? "success" : "muted"}>
                        {a.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </td>
                    <td className="text-right">
                      <DeactivateResourceActions
                        kind="agent"
                        id={a.id}
                        label={a.name}
                        isActive={a.is_active}
                        onChanged={load}
                        onError={setError}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
