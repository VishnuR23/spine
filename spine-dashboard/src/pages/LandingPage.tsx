import { Link } from "react-router";
import { Button } from "@/components/ui";

export function LandingPage() {
  return (
    <div className="min-h-screen bg-spine-bg">
      <header className="sticky top-0 z-10 border-b border-spine-border bg-spine-bg/80 backdrop-blur-xl">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-6">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-spine-border-strong bg-spine-elevated">
              <div className="h-3 w-3 rounded-sm bg-gradient-to-br from-indigo-400 to-violet-500" />
            </div>
            <span className="text-sm font-semibold text-spine-fg">Spine</span>
          </div>
          <div className="flex items-center gap-3">
            <Link to="/login" className="text-sm text-spine-muted hover:text-spine-fg transition-colors">
              Sign in
            </Link>
            <a href="mailto:vishnu@spinelayer.com?subject=Spine access request">
              <Button size="sm">Request access</Button>
            </a>
          </div>
        </div>
      </header>

      <main>
        <section className="mx-auto max-w-4xl px-6 pt-24 pb-20 text-center">
          <p className="mb-6 inline-flex items-center gap-2 rounded-full border border-spine-border bg-spine-surface px-3 py-1 text-xs text-spine-muted">
            <span className="h-1.5 w-1.5 rounded-full bg-spine-accent-strong" />
            AI agent governance platform
          </p>
          <h1 className="text-4xl font-semibold tracking-tight text-spine-fg sm:text-5xl lg:text-[3.25rem] lg:leading-[1.1]">
            Policy, audit, and oversight
            <br />
            <span className="text-spine-muted">for autonomous agents</span>
          </h1>
          <p className="mx-auto mt-6 max-w-xl text-base leading-relaxed text-spine-muted">
            Spine evaluates every agent action against your policies, records a tamper-evident audit
            trail, and routes high-risk decisions to human review.
          </p>
          <div className="mt-10 flex flex-wrap justify-center gap-3">
            <a href="mailto:vishnu@spinelayer.com?subject=Spine access request">
              <Button>Request access</Button>
            </a>
            <Link to="/login">
              <Button variant="secondary">Sign in</Button>
            </Link>
          </div>
        </section>

        <section className="mx-auto max-w-6xl px-6 pb-24">
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <FeatureCard
              label="Intercept"
              title="Real-time decisions"
              desc="Default-deny guardrails with explicit allow, deny, and flag rules before actions reach your systems."
            />
            <FeatureCard
              label="Audit"
              title="Tamper-evident log"
              desc="Hash-chained events you can verify anytime — built for SOC 2, HIPAA, and regulatory review."
            />
            <FeatureCard
              label="Plan-bound"
              title="Drift detection"
              desc="A context-isolated reviewer scores every action against the agent's declared plan and hard-blocks on drift."
            />
            <FeatureCard
              label="Approvals"
              title="Human-in-the-loop"
              desc="Flagged actions pause for operator review before the agent proceeds."
            />
          </div>
        </section>

        <section className="border-y border-spine-border bg-spine-surface/50">
          <div className="mx-auto max-w-3xl px-6 py-20 text-center">
            <h2 className="text-xl font-semibold text-spine-fg">How it works</h2>
            <p className="mt-3 text-sm leading-relaxed text-spine-muted">
              Your agent calls{" "}
              <code className="code-inline">POST /v1/intercept</code> with the action it wants to take.
              Spine returns allow, block, or flag — and records the decision regardless.
            </p>
            <pre className="mt-8 overflow-x-auto rounded-xl border border-spine-border bg-spine-bg p-5 text-left font-mono text-xs leading-relaxed text-spine-muted">
{`Agent → POST /v1/intercept
         ├─ Policy engine  → allow / deny / flag
         ├─ Audit logger   → hash-chained event
         └─ Response       → { allowed, reason }`}
            </pre>
          </div>
        </section>

        <section className="mx-auto max-w-2xl px-6 py-20 text-center">
          <h2 className="text-xl font-semibold text-spine-fg">Ready to govern your agents?</h2>
          <p className="mt-3 text-sm text-spine-muted">
            Org admins provision team access from Settings. Platform operators manage tenants from Organizations.
          </p>
          <a
            href="mailto:vishnu@spinelayer.com?subject=Spine access request"
            className="mt-8 inline-block"
          >
            <Button>Request access</Button>
          </a>
        </section>
      </main>

      <footer className="border-t border-spine-border py-6 text-center text-2xs text-spine-subtle">
        Spine — policy, audit, and plan-bound oversight for AI agent traffic.
      </footer>
    </div>
  );
}

function FeatureCard({ label, title, desc }: { label: string; title: string; desc: string }) {
  return (
    <div className="surface-panel p-6">
      <p className="text-2xs font-medium uppercase tracking-wider text-spine-accent">{label}</p>
      <p className="mt-2 text-sm font-medium text-spine-fg">{title}</p>
      <p className="mt-2 text-sm leading-relaxed text-spine-muted">{desc}</p>
    </div>
  );
}
