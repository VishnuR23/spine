# C++ SDK: connection pool, order lifecycle, circuit breaker

Status: approved in chat, 2026-09-30.

## Goal

Make `sdks/cpp` something a trading system would adopt: lower latency per
check, coverage of the whole order lifecycle, and visible, fast behavior when
Spine degrades.

## Constraints (unchanged from today)

- Depends only on libcurl and the C++17 standard library.
- Nothing on the order path throws.
- Spine decides; the client only chooses which question to ask.
- Existing public API keeps compiling and behaving the same, except where this
  spec says otherwise.
- No server changes. New behavior is expressed as new action types.

## 1. Connection pool

`Client` gains shared internal state (`std::shared_ptr<detail::Shared>`),
created in the constructor. Copies of a `Client` share it, so a pool, stats,
and breaker are per logical client, not per copy.

The pool is a mutex-protected stack of `CURL*` easy handles. A call checks one
out (or creates one if empty), resets per-request options, performs, and
returns it. libcurl keeps the TCP connection alive inside the handle and
reconnects transparently if the server closed it. Handles are cleaned up when
the shared state is destroyed. `Client` stays safe to use from many threads.

`Config::reuse_connections` (default `true`). When `false`, each call creates
and destroys its own handle, as today.

The pool has no size cap beyond peak concurrency: it holds at most as many
handles as were ever in use at once.

## 2. Order lifecycle

`Order` gains:

- `client_order_id` (string) — sent as metadata on every order check.
- `reference_price` (double, default 0) — used to price market orders.

`NotionalBands` gains `market_collar` (default `1.05`).

Pricing, used by `notional()` and band selection:

| limit_price | reference_price | priced at | metadata `pricing` |
|---|---|---|---|
| > 0 | any | quantity × limit_price | `limit` |
| <= 0 | > 0 | quantity × reference_price × market_collar | `reference` |
| <= 0 | <= 0 | unpriced | `unpriced` |

New `OrderGate` methods:

- `check_amend(const Order& original, const Order& amended, session_id = "")`
  — bands on the *amended* order's notional. Action types `order.amend`,
  `order.amend.large`, `order.amend.block`, `order.amend.unpriced`. Metadata
  carries the amended order's fields plus `prev_quantity`, `prev_limit_price`,
  `prev_notional`.
- `check_cancel(const Order& order, session_id = "")` — action type
  `order.cancel`, target the symbol, metadata `client_order_id`, `side`,
  `quantity`, `strategy_id`, `trader_id`.

`action_type_for(order, bands)` keeps its signature; a new
`action_type_for(prefix, order, bands)` overload serves amends.

### Cancels fail open

Blocking a cancel keeps risk on. So for `check_cancel` only:

- Transport failure, budget exceeded, unreadable response, or non-200 →
  `allowed = true`, `decision = Allowed`, `failed_closed = true` (the field
  means "decided locally"), reason prefixed `cancel allowed locally: `.
- Breaker open or manual halt → the same, without a network call when halted
  or open, reason `cancel allowed locally: circuit open` / `halted`.
- A real Spine verdict (HTTP 200, parsed) is returned as-is, including a
  deliberate block.

Implemented by a per-call `LocalVerdict` argument on an internal
`Client::intercept_impl`, not by flipping `Config::fail_open`.

### Policy pack (`examples/finance/seed_finance_policies.py`)

- Restricted-list deny also covers `order.amend*`.
- Amend bands mirror place bands: `order.amend.block` deny,
  `order.amend.large` and `order.amend.unpriced` flag, `order.amend` allow in
  trading hours.
- `order.cancel` allow, no time window.
- The trading mandate's expected action types include the new ones.

## 3. Stats and circuit breaker

### Stats

`Client::stats()` returns a `Stats` snapshot:

- counts: `allowed`, `blocked`, `flagged`, `failed_closed`, `short_circuited`
  (refused or allowed locally by an open breaker or halt, no network call)
- `latency_buckets`: upper bounds 1, 2, 5, 10, 20, 50, 100 ms, plus overflow
- `max_latency`

Counters are `std::atomic<uint64_t>`. Short-circuited calls count in
`short_circuited` and their decision bucket, not in latency.

### Breaker

States `Closed`, `Open`, `HalfOpen`. `Config` gains:

- `breaker_threshold` (default 5) — consecutive `failed_closed` results that
  trip it. 0 disables the breaker.
- `breaker_cooldown` (default 5 s).
- `on_breaker_change` — `std::function<void(BreakerState)>`, optional, called
  on the calling thread after the transition.

Behavior:

- Closed: every call goes to Spine. A real verdict resets the failure count; a
  local verdict increments it. Reaching the threshold → Open.
- Open: calls return the local verdict immediately (blocked, or allowed if
  `fail_open`; cancels allowed). After the cooldown, the next call becomes the
  single probe → HalfOpen.
- HalfOpen: one probe in flight; other calls short-circuit. Probe gets a real
  verdict → Closed; local verdict → Open, cooldown restarts.

`Client::breaker_state()` reports the state.

### Manual halt

`Client::halt(reason)` / `Client::resume()` / `Client::halted()`. While
halted, every non-cancel call returns blocked locally (`failed_closed = true`,
reason `halted: <reason>`) regardless of `fail_open`. Halt is independent of
the breaker.

## Testing

- Offline unit tests for pricing, band selection, amend/cancel action types
  and metadata, and the cancel local-verdict rule.
- `tests/fake_spine.hpp`: a test-only POSIX socket HTTP/1.1 server on a
  background thread, returning canned responses, counting accepted
  connections and requests. Used to prove: N calls reuse one connection with
  pooling on and open N with it off; stats counts and buckets; breaker trips
  at the threshold, short-circuits, probes after cooldown, recovers; halt
  blocks orders but not cancels.
- Existing tests keep passing unchanged.

## Out of scope

Position and exposure limits; order routing; async or batch APIs; any server
change; sessions API from C++.
