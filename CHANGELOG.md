# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- C++ SDK (`sdks/cpp`) for latency-sensitive callers, depending only on
  libcurl and the standard library. Hard latency budget per call, fail-closed
  by default, and `OrderGate` for pre-trade checks.
- A finance policy pack and trading mandate
  (`examples/finance/seed_finance_policies.py`): restricted list, notional
  bands with four-eyes approval, trading-hours window, and market-data
  entitlements, plus a runnable `pretrade_gate` example.
- `make test-cpp`, and a CI job building and testing the C++ SDK.

### Changed

- `pyproject.toml` is now the single source of dependencies. `requirements.txt`,
  `requirements-dev.txt`, and `setup.cfg` are removed — install with
  `pip install -e ".[dev]"`. The package previously declared no dependencies at
  all, so `pip install spine` produced an installation that could not import.
- The generated OpenAPI export moved from `openapi/openapi.json` to
  `docs/openapi.json`.

### Fixed

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
