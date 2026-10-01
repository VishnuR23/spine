# Spine C++ SDK

A pre-trade gate for systems that cannot afford a Python bridge on the order
path — order management, execution, and smart-order-routing stacks, which are
overwhelmingly C++.

Depends on libcurl and the standard library. Nothing else.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
./build/spine_tests
```

## Using it

```cpp
#include "spine/finance.hpp"

spine::Config cfg;
cfg.base_url = "http://127.0.0.1:8000";
cfg.org_key  = "spine_...";
cfg.timeout  = std::chrono::milliseconds(50);   // hard ceiling per call
cfg.fail_open = false;                          // leave this false

spine::finance::OrderGate gate(spine::Client(cfg), agent_id);

gate.pre_open_check();   // once, at start of day — see "Cold start" below

spine::finance::Order o{"AAPL", spine::finance::Side::Buy,
                        100, 150.00, "XNAS", "stat-arb", "trader-1"};
o.client_order_id = "co-1";   // ties amends and cancels back to this order

const auto verdict = gate.check(o, session_id);
if (verdict.allowed) {
    send_to_venue(o);
} else {
    reject(o, verdict.reason);   // "Restricted list", "Four-eyes", ...
}

// Later in the order's life:
gate.check_amend(o, resized, session_id);   // banded on the new size
gate.check_cancel(o, session_id);           // allowed even if Spine is down

// Operations:
const spine::Stats s = gate.client().stats();   // counts and latency
gate.client().halt("risk limit breached");      // desk kill switch
gate.client().resume();
```

## Three decisions worth explaining

### Fail closed

If Spine cannot be reached, or the latency budget is exceeded, the answer is
"do not send the order". A control that disappears under load is not a
control, and load is exactly when it disappears.

`fail_open` exists, and defaults to false. Turning it on means deciding that
unsupervised trading is the safer failure. Occasionally that is true. Usually
it is not, and it should be a decision someone signed off on rather than a
default nobody noticed.

Every locally-produced verdict sets `Result::failed_closed`. Count it
separately from genuine rejections: a rising count means the control is
degrading even though orders are still being refused correctly.

### A latency budget, not a timeout

The trading system's deadline is fixed, so a gate that occasionally takes two
seconds is worse than one that reliably takes 50 ms and refuses.
`Config::timeout` bounds the entire call, not just the connect phase.

Measured against a local Docker deployment over loopback:

| | latency |
|---|---|
| First request into a cold stack | 55–145 ms |
| Steady state | 6–13 ms |

The steady state has comfortable headroom under a 50 ms budget. The cold start
does not, which is what `pre_open_check` is for.

### Cold start, and why a health check does not fix it

The first request into a cold stack is roughly ten times slower than the
steady state. Under a 50 ms budget that means the first order of the session
fails closed — safe, but useless: it stops the one order nobody wanted stopped
and teaches the desk to distrust the gate.

The obvious fix is to ping `/health` at startup. **It does not work**, and it
is worth knowing why: what is cold is authentication, the policy cache, and
the audit write path, and `/health` touches none of them. Measured, a health
check made no difference at all — the next four orders still exceeded budget.

Worse, there is a feedback trap. A request aborted at 50 ms warms nothing, so
a too-tight budget prevents the very warming that would make it fast, and the
gate stays cold indefinitely.

`OrderGate::pre_open_check()` runs one real intercept through the full path
with a budget large enough to complete. After it, the same four orders came
back in 12, 8, 13, and 10 ms. It leaves a row in the audit log, deliberately —
desks already send a test ticket through risk before the open, and a record
that the control was exercised is a feature.

## Notional bands, and why they exist

Spine's policy engine matches strings: action type, a regex over the target
resource, and a UTC time window. It cannot compare numbers, so it cannot
express "block orders above ten million".

The gate resolves this by moving the arithmetic to the caller and leaving the
authority on the server. It computes notional and picks an action type:

| Order | Action type | Shipped policy |
|---|---|---|
| Below the review threshold | `order.place` | allow, during trading hours |
| At or above 1M | `order.place.large` | flag — four-eyes approval |
| At or above 10M | `order.place.block` | deny |
| No limit price | `order.place.unpriced` | flag — notional is unbounded |

Thresholds are in `NotionalBands`; the policy governing each band lives in
Spine, so compliance can change what a band *means* without anyone rebuilding
the trading system.

The unpriced case is the one that matters. A market order has no limit price,
so its notional is unknown. Treating unknown as zero would let it slip under
every band — the failure mode where a market order becomes the cheapest route
past a control. It is routed for review instead.

## Amends, cancels, and market orders

**Amends** are a new risk decision at the new size, so they are banded on the
amended order's notional and use their own action types. The previous
quantity, price, and notional go with them into the audit trail.

| Amended order | Action type | Shipped policy |
|---|---|---|
| Below the review threshold | `order.amend` | allow, during trading hours |
| At or above 1M | `order.amend.large` | flag — four-eyes approval |
| At or above 10M | `order.amend.block` | deny |
| No limit or reference price | `order.amend.unpriced` | flag |

The restricted list and the trading-hours window cover amends exactly as
they cover new orders.

**Cancels** are the one call that does not fail closed. Refusing a cancel
keeps the position on, which is the opposite of safe. `check_cancel` asks
Spine as usual, and a real verdict stands — including a deliberate block,
if your policy says so. But if the answer would be produced locally (Spine
unreachable, budget exceeded, breaker open, halted), the cancel is allowed,
with `failed_closed` set and the reason prefixed `cancel allowed locally: `.
The shipped pack allows `order.cancel` in any name, at any hour.

**Market orders** can carry a `reference_price` — the last trade or mid you
would mark them at. With one, the order is sized like a limit order, marked
up by `NotionalBands::market_collar` (default 1.05) to cover slippage, and
banded normally. Without one it is still `unpriced` and goes to review. The
reference is yours to keep fresh: a stale reference is a wrong notional.

Every order check sends `pricing` (`limit`, `reference`, or `unpriced`) and,
when used, the `reference_price`, so the audit trail shows how each order
was sized.

## When Spine degrades

**Circuit breaker.** After `Config::breaker_threshold` consecutive local
verdicts (default 5), the breaker opens: calls are answered locally at once,
with no network call, instead of each one waiting out the full budget.
After `breaker_cooldown` (default 5 s) one probe is let through; a real
verdict closes the breaker, a failure re-opens it. Set the threshold to 0 to
disable it. `Config::on_breaker_change` is called on each transition — wire
it to alerting. Exceptions it throws are swallowed, because nothing on the
order path throws.

While open, orders are refused (or allowed, if you set `fail_open`) with the
reason `circuit open`. Cancels are allowed.

**Manual halt.** `client().halt(reason)` stops every new order and amend
locally, with no network call, until `resume()`. It ignores `fail_open`: a
halt is a decision, not a failure. Cancels still go through. Copies of a
`Client` share the halt, the breaker, and the stats.

**Stats.** `client().stats()` returns counters that are cheap enough to
leave on:

- `allowed`, `blocked`, `flagged` — every call lands in exactly one, by its
  final decision.
- `failed_closed` — went to Spine, but the verdict was produced locally.
  A rising count means the control is degrading.
- `short_circuited` — answered locally with no network call (breaker open,
  or halted).
- `latency_buckets` — network calls only, with inclusive upper bounds of 1,
  2, 5, 10, 20, 50, and 100 ms, plus one for anything slower; and
  `max_latency`.

## What stays on the server

The gate holds no restricted list. It sends the symbol as the target resource
and lets Spine match it, because a restricted list changes intraday and a copy
compiled into a trading binary is a copy that is out of date. Same for
entitlements, trading hours, and thresholds.

The client decides which question to ask. It never decides the answer.

## Metadata and MNPI

Order metadata lands verbatim in the hash-chained audit log. That is the
point — it is the record of what was asked and on whose behalf.

For the same reason, keep it to trade facts: side, quantity, price, notional,
venue, strategy, trader. No client identifiers, no research text, no rationale
prose. Spine redacts common secret shapes on the way through, but that is a
safety net, not a licence to send material non-public information or personal
data into an append-only store.

## What this does not do

- **Position and exposure limits.** They need a view of the book that this
  client does not have. That belongs in a risk system.
- **Order routing or execution.** Spine answers whether the agent was
  permitted to act, and leaves the proof.

## Connections

Connections are kept open and reused between calls by default, from a pool
shared by every copy of a `Client`, so the class stays safe to use from many
threads. A connection that timed out or errored is dropped rather than
reused. Set `Config::reuse_connections = false` to open a fresh connection
per call instead.

## Running the example

```bash
make demo                                                   # from the repo root
python examples/finance/seed_finance_policies.py --always-open
export SPINE_ORG_KEY=...  SPINE_AGENT_ID=...  SPINE_SESSION_ID=...
./build/pretrade_gate
```

`--always-open` widens the trading window to 24 hours so the demo behaves the
same outside market hours. Drop it to see out-of-hours orders refused by the
window policy.

Passing `SPINE_SESSION_ID` also puts each order under the declared mandate, so
plan-bound review scores it — which is what catches an agent whose individual
orders are all permitted but whose trajectory has drifted from its strategy.
