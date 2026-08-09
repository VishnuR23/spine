# Spine

**A control plane for autonomous AI agents.** Every action an agent wants to take —
reading a file, running a shell command, calling an API — goes through Spine first.
Spine decides, records the decision in a tamper-evident log, and can escalate to a
human when an agent starts drifting from what it was asked to do.

```
agent  ──▶  POST /v1/intercept  ──▶  allow / block / flag  ──▶  agent proceeds or stops
                    │
                    ├──▶  hash-chained audit log
                    └──▶  plan reviewer (async) ──▶  human approval when the agent drifts
```

Two layers, and that is the whole system:

1. **Deterministic policy** — regex and time-window rules, evaluated synchronously,
   p99 under 50 ms. No model in the loop. Default-deny.
2. **Plan-bound review** — a separate LLM call, off the hot path, that compares what
   the agent is *doing* against what it *said it would do*.

---

## Try it in one command

```bash
make demo
```

Brings up Postgres, Redis, the API, the worker, and the dashboard; seeds an
organization, an agent, and three policies; creates a login and prints it.
Then open http://localhost:4173.

Want to see the two interesting parts without Docker? These run standalone,
no API key required:

```bash
python examples/audit_tamper_detection.py   # edit the audit log, watch it get caught
python examples/injection_isolation.py      # try to prompt-inject the reviewer, watch it fail
```

Each prints what it is doing at every step. They are the fastest way to
understand what Spine actually does.

---

## Why two layers

Policy engines are predictable but literal. They can tell you `rm -rf /var/log`
is forbidden. They cannot tell you that an agent asked to *"summarize last week's
tickets"* has spent the last twenty steps reading the payroll directory — every
one of those reads is individually permitted.

Model-based monitors catch that, but they inherit a problem: if the monitor reads
the agent's context to judge it, then anything that reaches the agent's context can
address the monitor. A prompt injection sitting in a file the agent opened is now
talking to your safety system.

Spine runs both, and keeps them apart. The policy engine handles what can be
written as a rule. The plan reviewer handles intent — and it never reads agent
context, so there is nothing for an injection to ride in on.

### The audit log

Each row's hash covers its own contents plus the previous row's hash. Editing
history means recomputing every hash after the edit, so `GET /v1/audit/verify`
detects tampering and reports the first row that broke.

`examples/audit_tamper_detection.py` demonstrates this against a real database.

### The plan reviewer

Before it starts work, an agent declares a plan: a goal, constraints, the resources
it expects to touch, and what success looks like. Every subsequent action is scored
against that plan by a separate model call that receives **only** the declared plan,
the current action's type and target, and a summary of prior verdicts.

It does not receive file contents, tool outputs, the agent's prompts, or metadata.
Not because those are filtered — because the function that builds its input has no
parameter for them. Verdicts accumulate into a drift score; crossing one threshold
opens a human approval ticket, crossing a higher one blocks further actions in that
session.

`examples/injection_isolation.py` demonstrates this. Details in
[docs/PLAN_BOUND_MONITORING.md](docs/PLAN_BOUND_MONITORING.md).

---

## Connecting an agent

Spine is middleware — your agent asks before it acts.

```python
import httpx
from spine_sdk import SpineClient, SpineBlockedError, require_allowed

spine = SpineClient(
    base_url="http://localhost:8000",
    org_key="spine_...",
    http_client=httpx.Client(),
)

try:
    require_allowed(spine, agent_id=agent_id, action_type="read", target_resource="/data/report.csv")
except SpineBlockedError as e:
    print(f"Spine said no: {e.reason}")
else:
    contents = open("/data/report.csv").read()
```

- **Claude Code** — drop-in `PreToolUse` hook: `cd integrations/claude-code-spine && ./install.sh`
- **Python / LangGraph** — [`sdks/python`](sdks/python/), with a `guard_tool` decorator
- **TypeScript** — [`sdks/ts`](sdks/ts/)
- **C++** — [`sdks/cpp`](sdks/cpp/), for latency-sensitive systems. See below.
- **Anything else** — one HTTP POST. See [docs/INTEGRATING_AGENTS.md](docs/INTEGRATING_AGENTS.md).

### Trading systems: the C++ pre-trade gate

Agents that place orders sit inside execution stacks written in C++, on a
fixed deadline, where a control that stalls is worse than one that refuses.
[`sdks/cpp`](sdks/cpp/) is a client built for that: libcurl and the standard
library, a hard latency budget per call, and fail-closed by default.

On top of it, `spine::finance::OrderGate` turns an order into an intercept:

```cpp
spine::finance::OrderGate gate(spine::Client(cfg), agent_id);
gate.pre_open_check();                       // warm the path before the open

const auto verdict = gate.check(order, session_id);
if (!verdict.allowed) reject(order, verdict.reason);
```

The design problem worth reading the code for: Spine's policy engine matches
strings, so it cannot express "block orders above ten million". The gate
computes notional locally and picks an action type — `order.place`,
`order.place.large`, `order.place.block` — while the policy governing each
band stays on the server, where compliance can change it without rebuilding
the trading system. Unpriced market orders get their own band rather than
being treated as zero, which would make a market order the cheapest way past
a limit.

Restricted lists work the same way: the gate sends the symbol and holds no
list of its own, because a restricted list changes intraday and a copy
compiled into a binary is a copy that is wrong.

A runnable pack of finance policies — restricted list, notional bands,
four-eyes approval, trading-hours window, market-data entitlements — plus a
declared trading mandate:

```bash
make demo
python examples/finance/seed_finance_policies.py --always-open
./sdks/cpp/build/pretrade_gate
```

Details, including measured latency and why a `/health` warm-up does *not*
fix cold start, are in [`sdks/cpp/README.md`](sdks/cpp/README.md).

---

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit, and why they are arranged this way |
| [docs/PLAN_BOUND_MONITORING.md](docs/PLAN_BOUND_MONITORING.md) | The plan reviewer in depth |
| [docs/INTEGRATING_AGENTS.md](docs/INTEGRATING_AGENTS.md) | Connecting agents and frameworks |
| [docs/API.md](docs/API.md) | Endpoint reference and curl examples |
| [deploy/vps/README.md](deploy/vps/README.md) | Production deploy on a single VPS |
| [CLAUDE.md](CLAUDE.md) | Operating manual: invariants, performance budgets, known traps |

`CLAUDE.md` is written for AI coding assistants working in this repo, but it is
the most direct description of the system's constraints and is worth reading
if you plan to contribute.

---

## Development

```bash
make verify    # everything: backend tests, hook tests, smoke test, dashboard build
make test      # backend + hook tests only
make smoke     # end-to-end against a real server (~30s, no API key needed)
make logs      # tail API and worker logs
make clean     # tear down and destroy local data
```

Requires Python 3.12+, Node 20+, and Docker.

The smoke test is the highest-signal single check — it boots a real server,
runs a full session lifecycle, drives the drift gate, and invokes the Claude Code
hook as a subprocess.

---

## Status

Spine is young, and honest about it. What is solid: the intercept path, the
policy engine, the audit chain, sessions and the plan reviewer, approvals,
webhooks, the dashboard, and the Claude Code integration. Everything in the test
suite runs on every commit.

Known limitations:

- The plan reviewer is asynchronous. It scores an action after it happened and
  opens an approval; the *hard block* is synchronous, but it applies to the
  session's next action rather than the one that triggered it. A synchronous
  reviewer on the hot path is a natural extension and is not built.
- The policy language is regex, action type, and time window. Expressive enough
  to be useful, not a general-purpose policy language.
- Multi-tenancy is enforced in application code, not Postgres row-level security.
- Single-node deploy today. The architecture is stateless and horizontally
  scalable, but that has not been exercised under real load.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests welcome —
particularly around policy expressiveness, additional framework integrations,
and adversarial testing of the reviewer.

To report a security issue, see [SECURITY.md](SECURITY.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
