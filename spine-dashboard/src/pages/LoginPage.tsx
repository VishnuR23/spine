import { FormEvent, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router";
import { useAuth } from "@/contexts/AuthContext";
import { Alert, Button, Field, Input } from "@/components/ui";

export function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { ready, user, login } = useAuth();

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-spine-bg text-spine-muted">
        Loading…
      </div>
    );
  }
  if (user) return <Navigate to="/dashboard" replace />;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(email, password);
      navigate("/dashboard", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col bg-spine-bg">
      <header className="border-b border-spine-border px-6 py-4">
        <Link to="/welcome" className="inline-flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-spine-border-strong bg-spine-elevated">
            <div className="h-3 w-3 rounded-sm bg-gradient-to-br from-indigo-400 to-violet-500" />
          </div>
          <span className="text-sm font-semibold text-spine-fg">Spine</span>
        </Link>
      </header>

      <div className="flex flex-1 items-center justify-center px-4 py-16">
        <div className="w-full max-w-[400px]">
          <h1 className="text-2xl font-semibold tracking-tight text-spine-fg">Sign in</h1>
          <p className="mt-2 text-sm text-spine-muted">Access your organization control plane.</p>

          <form onSubmit={onSubmit} className="mt-8 space-y-5">
            <Field label="Email">
              <Input
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                required
              />
            </Field>
            <Field label="Password">
              <Input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
            </Field>
            {error ? <Alert variant="error">{error}</Alert> : null}
            <Button type="submit" disabled={loading} className="w-full">
              {loading ? "Signing in…" : "Continue"}
            </Button>
          </form>

          <p className="mt-8 text-center text-xs text-spine-subtle">
            Need access?{" "}
            <a
              href="mailto:vishnu@spinelayer.com?subject=Spine access request"
              className="text-spine-muted hover:text-spine-fg underline-offset-2 hover:underline"
            >
              Request an invite
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
