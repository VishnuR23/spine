# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A `spine.plan.review_failed` webhook, sent when the plan reviewer fails
  closed and hands an action to a human. See the Webhooks section of
  `docs/API.md`.
- C++ SDK (`sdks/cpp`) for latency-sensitive callers, depending only on
  libcurl and the standard library. Hard latency budget per call, fail-closed
  by default, and `OrderGate` for pre-trade checks.
- A finance policy pack and trading mandate
  (`examples/finance/seed_finance_policies.py`): restricted list, notional
  bands with four-eyes approval, trading-hours window, and market-data
  entitlements, plus a runnable `pretrade_gate` example.
- `make test-cpp`, and a CI job building and testing the C++ SDK.
- C++ SDK: connections are pooled and reused by default
  (`Config::reuse_connections`), shared across copies of a `Client`.
- C++ SDK: the full order lifecycle. `OrderGate::check_amend` bands an amend
  on its new size (`order.amend*`); `OrderGate::check_cancel` sends
  `order.cancel` and allows the cancel when the verdict would be local,
  because refusing a cancel keeps risk on. Market orders can be sized off a
  `reference_price` with a slippage collar. The finance policy pack covers
  amends and cancels.
- C++ SDK: `Client::stats()` (decision counts, failed-closed and
  short-circuited counts, latency histogram), a circuit breaker that answers
  locally while Spine keeps failing, and a manual `halt()` kill switch.

### Changed

- Spine is now positioned finance-first: pre-trade controls and a provable
  audit trail for AI trading agents. The README, landing page, dashboard
  wording, and docs lead with the trading use case; the core stays
  domain-neutral, and a "Works for any agent" section covers the rest.
- `make demo` now seeds a trading desk — the finance policy pack (open 24h)
  and a declared trading mandate — instead of generic file and shell rules,
  and prints the exports the C++ pre-trade gate needs. The API image now
  includes `examples/`.
- The plan reviewer now fails closed. When every retry of a review fails
  (bad JSON, a refusal, or an API error), the worker opens a pending
  approval for the action and writes a `plan.evaluation_failed` audit row,
  instead of silently dropping the review and leaving the action
  unreviewed. The session's drift score is not changed.

- `pyproject.toml` is now the single source of dependencies. `requirements.txt`,
  `requirements-dev.txt`, and `setup.cfg` are removed — install with
  `pip install -e ".[dev]"`. The package previously declared no dependencies at
  all, so `pip install spine` produced an installation that could not import.
- The generated OpenAPI export moved from `openapi/openapi.json` to
  `docs/openapi.json`.
- The plan reviewer's default model (`MONITOR_MODEL`) is now
  `claude-sonnet-5-5`, replacing `claude-sonnet-4-20250514`.
- In production, the API now refuses to start when `JWT_SECRET` is shorter
  than 32 bytes, the minimum for an HS256 key (RFC 7518 §3.2). Secrets from
  `tools/init_env.py` already meet this.

### Fixed

- Order checks stalled when Redis was down. With Redis refusing connections,
  a session-bound intercept took ~650 ms (Celery's publish retries, run
  inside the request); with Redis unreachable, the policy cache read had no
  connect timeout and hung. Either exceeded the C++ gate's 50 ms budget, so
  every order failed closed. Hot-path Redis calls now time out at 50 ms, do
  not retry, and skip Redis for 5 s after a failure
  (`REDIS_HOT_PATH_TIMEOUT_MS`, `REDIS_RETRY_AFTER_SECONDS`).
- Tests could not be collected under a plain `pytest` invocation, because the
  repository root was not on `sys.path`. Only `python -m pytest` worked.
- Alembic revision `0013` used a 38-character identifier, exceeding the
  32-character `alembic_version.version_num` column. The API failed to start
  against Postgres; SQLite does not enforce the limit, so the test suite passed
  regardless.
- A fresh clone brought up a crash-looping dashboard: the placeholder
  `SESSION_SECRET` shipped in `.env.example` is on the BFF's rejected-secrets
  list. `make demo` now generates real secrets via `tools/init_env.py`.
- Both SDKs exposed an `egress_http` method targeting `/v1/egress/http`, an
  endpoint that no longer exists.
- `ruff` was unpinned, so CI installed whichever release was newest and could
  fail with no change to the code.

### Security

- The `JWT_SECRET` placeholder in `deploy/vps/env.production.example` was not
  on the rejected-secrets list, so a deployment that left it unedited started
  normally with a publicly known signing key.
- Dashboard dependencies updated to clear every `npm audit` advisory:
  `express` 4.22.3 (patched `qs`), `body-parser` 1.20.8, `react-router` 7.18
  (replacing `react-router-dom` 6), and `vite` 6.4 for the dev server.

## [0.1.0] — 2026-08-06

Initial public release.

- `POST /v1/intercept` with a deterministic policy engine (action type, target
  regex, time window), default-deny, evaluated synchronously.
- Hash-chained audit log with on-demand verification via `GET /v1/audit/verify`.
- Plan-bound monitoring: sessions carry a declared plan, and a context-isolated
  reviewer scores each action against it on the Celery worker, accumulating a
  drift score that opens approval tickets and blocks sessions past a threshold.
- Human approvals with time-boxed grants.
- HMAC-signed webhooks with SSRF validation.
- Live event stream over SSE.
- React dashboard behind an Express BFF.
- Python and TypeScript SDKs, and a Claude Code `PreToolUse` hook.
- Single-VPS deployment configuration.
