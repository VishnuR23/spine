# Spine

**Pre-trade controls and a provable audit trail for AI trading agents.**

An agent that can place orders needs the same controls a human trader works
under: a restricted list, size limits, four-eyes sign-off on large tickets,
trading hours, market-data entitlements, and a kill switch. It also needs
something a human desk does not: a check that it is still trading the
mandate it was given, rather than drifting somewhere every individual order
happens to be allowed.

Spine sits in front of the order path. The agent asks before every order,
amend, or cancel; Spine answers allow, block, or "needs a human", and writes
the question and the answer into a hash-chained audit log that anyone can
verify after the fact.

```
trading agent ──▶ C++ OrderGate ──▶ POST /v1/intercept ──▶ allow / block / four-eyes
                                          │
                                          ├──▶ hash-chained audit log
                                          └──▶ mandate reviewer (async) ──▶ human when the agent drifts
```

Two layers, and that is the whole system:

1. **Pre-trade policy** — restricted list, notional bands, trading-hours
   windows, entitlements. Deterministic, evaluated on every order, p99 under
   50 ms on the server. Default-deny: anything no policy allows is refused.
2. **Mandate review** — a separate model call, off the order path, that
   compares what the agent is *doing* against the mandate it *declared*.

---

## Try it

```bash
make demo
```

Brings up Postgres, Redis, the API, the worker, and the dashboard, seeds a
trading desk — a restricted list, notional bands with four-eyes approval,
market-data entitlements, and a declared rebalancing mandate — and prints a
login. Open http://localhost:4173.

Then send orders through the C++ gate:

```bash
cmake -S sdks/cpp -B sdks/cpp/build && cmake --build sdks/cpp/build
export SPINE_ORG_KEY=...  SPINE_AGENT_ID=...  SPINE_SESSION_ID=...   # printed by make demo
./sdks/cpp/build/pretrade_gate
```

Five orders, an amend, and a cancel: an ordinary order goes through, the
restricted name is refused, the large ticket waits for a second pair of eyes,
the unpriced market order goes to review, and the cancel is allowed. Run it
with Spine stopped and every order is refused instead — the gate fails
closed — while the cancel still goes through.

Two standalone scripts show the parts that are hard to believe until you see
them. Neither needs Docker or an API key:

```bash
python examples/audit_tamper_detection.py   # edit the audit log, watch it get caught
python examples/injection_isolation.py      # try to prompt-inject the reviewer, watch it fail
```

---

## The C++ pre-trade gate

Execution stacks are C++, on a fixed deadline, where a control that stalls is
worse than one that refuses. [`sdks/cpp`](sdks/cpp/) is built for that:
libcurl and the standard library only, a hard latency budget per check, and
fail-closed by default.

```cpp
spine::finance::OrderGate gate(spine::Client(cfg), agent_id);
gate.pre_open_check();                        // warm the path before the open

const auto verdict = gate.check(order, session_id);
if (!verdict.allowed) reject(order, verdict.reason);

gate.check_amend(order, resized, session_id); // banded on the new size
gate.check_cancel(order, session_id);         // allowed even if Spine is down
gate.client().halt("risk limit breached");    // desk kill switch
```

What it does on the order path:

- **Notional bands.** Spine's policy engine matches strings and cannot compare
  numbers, so the gate computes notional and picks an action type —
  `order.place`, `.large`, `.block`. The policy for each band lives on the
  server, where compliance can change it without rebuilding the trading
  system. The client chooses the question; it never decides the answer.
- **Market orders are not free.** An unpriced order is sent for review rather
  than treated as zero. With a reference price it is sized normally, plus a
  slippage collar.
- **Restricted list stays central.** The gate sends the symbol and holds no
  list of its own: a restricted list changes intraday, and a copy compiled
  into a binary is a copy that is wrong.
- **Fail closed, except cancels.** If Spine cannot answer within budget, new
  orders and amends are refused. Cancels are allowed, because refusing a
  cancel keeps the risk on.
- **Degradation you can see.** Connections are pooled. A circuit breaker
  answers locally once Spine keeps failing, instead of every order waiting out
  its budget. In-process stats count decisions, failed-closed checks, and
  latency, and a manual halt stops new orders on demand.

Measured over loopback against a local deployment: 6–13 ms per check once
warm. Details, including why a `/health` warm-up does not fix cold start, are
in [`sdks/cpp/README.md`](sdks/cpp/README.md). The policy pack the demo uses
is [`examples/finance/seed_finance_policies.py`](examples/finance/seed_finance_policies.py).

---

## Why two layers

Pre-trade rules are predictable but literal. They can refuse a restricted
name or a ten-million-dollar ticket. They cannot tell you that an agent asked
to *"rebalance the US equity book toward target weights"* has spent the last
twenty orders building a position in a name it was never meant to hold —
every one of those orders is individually within limits.

A model can catch that, but a model that reads the agent's context to judge
it inherits a problem: anything that reaches the agent's context can address
the monitor. A prompt injection in a research note the agent opened is now
talking to your risk control.

Spine runs both and keeps them apart. Policy handles what can be written as a
rule. The mandate reviewer handles intent — and it never reads agent context,
so there is nothing for an injection to ride in on.

### The audit log

Each row's hash covers its own contents plus the previous row's hash. Editing
history means recomputing every hash after the edit, so `GET /v1/audit/verify`
detects tampering and reports the first row that broke. Every order the agent
asked about, what Spine answered, and on whose behalf, is in that chain.

`examples/audit_tamper_detection.py` demonstrates this against a real database.

### The mandate reviewer

Before it starts trading, an agent declares a mandate: a goal, constraints,
the names it expects to trade, and what success looks like. Every subsequent
order is scored against that mandate by a separate model call that receives
**only** the declared mandate, the current action's type and target, and a
summary of prior verdicts.

It does not receive research, tool outputs, the agent's prompts, or order
metadata. Not because those are filtered — because the function that builds
its input has no parameter for them. Verdicts accumulate into a drift score;
crossing one threshold opens a human approval ticket, crossing a higher one
blocks the session's further orders. If the reviewer itself cannot produce a
verdict, the order is handed to a human rather than waved through.

`examples/injection_isolation.py` demonstrates this. Details in
[docs/PLAN_BOUND_MONITORING.md](docs/PLAN_BOUND_MONITORING.md).

### On regulation

Rules such as SEC Rule 15c3-5 (market access) and MiFID II RTS 6 (algorithmic
trading) expect pre-trade controls of this kind: maximum order value,
restricted instruments, a kill switch, and records of what was checked.
Spine provides mechanisms for those controls and evidence that they ran. It is
not a certified compliance product, and using it does not by itself make a
firm compliant with anything.

---

## Works for any agent

Nothing in the core is finance-specific: an action is a type, a target, and
some metadata, and policies match on those. The same layers govern agents
that read files, run shell commands, or call internal APIs.

```python
import httpx
from spine_sdk import SpineClient, SpineBlockedError, require_allowed

spine = SpineClient(base_url="http://localhost:8000", org_key="spine_...", http_client=httpx.Client())

try:
    require_allowed(spine, agent_id=agent_id, action_type="read", target_resource="/data/report.csv")
except SpineBlockedError as e:
    print(f"Spine said no: {e.reason}")
```

- **C++** — [`sdks/cpp`](sdks/cpp/), the pre-trade gate above
- **Python / LangGraph** — [`sdks/python`](sdks/python/), with a `guard_tool` decorator
- **TypeScript** — [`sdks/ts`](sdks/ts/)
- **Claude Code** — drop-in `PreToolUse` hook: `cd integrations/claude-code-spine && ./install.sh`
- **Anything else** — one HTTP POST. See [docs/INTEGRATING_AGENTS.md](docs/INTEGRATING_AGENTS.md).

---

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit, and why they are arranged this way |
| [sdks/cpp/README.md](sdks/cpp/README.md) | The C++ pre-trade gate: bands, cancels, breaker, latency |
| [docs/PLAN_BOUND_MONITORING.md](docs/PLAN_BOUND_MONITORING.md) | The mandate reviewer in depth |
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
make verify    # everything: backend, hook, and C++ tests, smoke test, dashboard build
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
policy engine, the audit chain, mandates and the reviewer, approvals, webhooks,
the dashboard, the C++ pre-trade gate, and the Claude Code integration.
Everything in the test suite runs on every commit.

Known limitations:

- The mandate reviewer is asynchronous. It scores an order after it was
  checked and opens an approval; the *hard block* is synchronous, but it
  applies to the session's next order rather than the one that triggered it.
  A synchronous reviewer on the order path is a natural extension and is not
  built.
- No position or exposure limits. They need a view of the book, which belongs
  in a risk system; Spine governs whether the agent was permitted to act.
- Trading-hours windows are fixed UTC times, so they drift by an hour across
  daylight-saving changes unless re-seeded.
- The policy language is regex, action type, and time window. Expressive enough
  to be useful, not a general-purpose policy language.
- Multi-tenancy is enforced in application code, not Postgres row-level security.
- Single-node deploy today. The architecture is stateless and horizontally
  scalable, but that has not been exercised under real load.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests welcome —
particularly around policy expressiveness (numeric comparisons would remove
the need for notional bands), exchange-local trading calendars, additional
framework integrations, and adversarial testing of the reviewer.

To report a security issue, see [SECURITY.md](SECURITY.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
