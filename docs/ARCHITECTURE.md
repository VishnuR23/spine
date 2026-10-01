# Architecture

Spine sits between an agent and the systems it acts on. This document explains
how the pieces fit together and why they are arranged this way.

---

## The request path

```
                          ┌──────────────────────────────────────┐
   agent                  │  API  (FastAPI, stateless)           │
     │                    │                                      │
     │  POST /v1/intercept│  1. authenticate  (org key or JWT)   │
     ├───────────────────▶│  2. drift gate    (session blocked?) │
     │                    │  3. policy engine (regex + time)     │
     │                    │  4. audit write   (hash-chained)     │
     │  allow/block/flag  │  5. enqueue review (if session_id)   │
     │◀───────────────────│  6. webhook       (fire and forget)  │
     │                    └───────────┬──────────────────────────┘
     │                                │
     │                          ┌─────▼─────┐      ┌──────────────┐
     │                          │   Redis   │◀────▶│  Dashboard   │
     │                          │ queue +   │ SSE  │  (React+BFF) │
     │                          │ cache +   │      └──────────────┘
     │                          │ pub/sub   │
     │                          └─────┬─────┘
     │                                │
     │                    ┌───────────▼──────────────────────────┐
     │                    │  Worker  (Celery)                    │
     │                    │                                      │
     │                    │  plan reviewer → verdict → drift     │
     │                    │  score → approval ticket if over     │
     │                    │  threshold                           │
     │                    └───────────┬──────────────────────────┘
     │                                │
     │                          ┌─────▼─────┐
     └──────────────────────────│ Postgres  │
        blocked on next call    └───────────┘
        if drift crossed the
        block threshold
```

Steps 1–6 are the hot path and target p99 under 50 ms. Nothing in that path
calls a model or waits on an external service.

---

## Why the model call is not on the hot path

An agent may call `/v1/intercept` before every single tool use. If a model call
sat in that path, every agent action would inherit model latency and model
availability. Instead the API enqueues a Celery task and returns.

The consequence is honest and worth stating: the reviewer's verdict on action N
arrives after action N has already been allowed. What the reviewer protects
against is the *trajectory*, not the individual step — it opens an approval
ticket, and if accumulated drift crosses the block threshold, the session's
next action is refused synchronously before policy evaluation even runs.

For the cases where the first divergent action itself must be stopped, a
synchronous reviewer would be the extension. It is not built.

---

## Components

### `spine/api/` — HTTP surface

One router per resource. Routes do authentication, validation, and delegation;
business logic lives in `spine/core/`. Auth resolves to an `org_id`, and every
query filters on it — multi-tenancy is enforced in application code.

### `spine/core/` — business logic, no HTTP

| Module | Responsibility |
|---|---|
| `intercept_service.py` | The pipeline above, in order |
| `policy_engine.py` | Pure function: rule config + action → allow/deny/flag |
| `policy_cache.py` | Redis-backed per-org policy cache, 300s TTL, explicit invalidation |
| `audit_logger.py` / `sync_audit_logger.py` | Hash-chained writes (async and worker variants) |
| `audit_verify.py` | Recomputes the chain for `GET /v1/audit/verify` |
| `approval_grants.py` | Time-boxed grants issued after a human approves |
| `webhook_dispatch.py` | HMAC-signed delivery to registered URLs |
| `redaction.py` | Strips secret-shaped values before they reach logs, SSE, or the reviewer |
| `idempotency.py` | `Idempotency-Key` replay protection |
| `event_bus.py` | Redis pub/sub backing the dashboard's live stream |

The audit hash formula appears in three files — two writers and one verifier —
and they must agree byte for byte. That duplication is deliberate (the worker
runs sync, the API runs async) and is the single most fragile thing in the
codebase. See invariant 1 in [CLAUDE.md](../CLAUDE.md).

### `spine/monitor/` — the plan reviewer

`plan_engine.py` runs on the worker: loads the session and the audit event,
summarizes prior verdicts, calls the model, persists the verdict, updates the
drift score, and opens an approval when warranted.

`prompts/plan_alignment.py` builds the model's input. Its signature is the
security boundary — see [PLAN_BOUND_MONITORING.md](PLAN_BOUND_MONITORING.md).

### `spine/worker/` — Celery

All long-running work. The API process never spawns background threads, which
is what makes horizontal scaling a matter of adding worker containers.

### `spine-dashboard/` — React + Vite + an Express BFF

The BFF holds the session cookie and proxies to the API, so the browser never
sees an org key. It deliberately does not hold the platform admin key.

---

## Data model

```
organizations ──┬── agents ──── audit_events ──── plan_evaluations
                ├── api_keys                │
                ├── policies                └──── approvals ──── approval_grants
                ├── users / memberships
                ├── sessions  (declared plans + drift score)
                └── webhooks
```

Every user-data table carries `org_id`. `audit_events` is the append-only spine:
one row per intercept, plus one per plan evaluation, indexed on
`(org_id, timestamp)` and `(org_id, sequence)`.

Migrations are Alembic, applied automatically when the API container starts.

---

## Failure behavior

The rule is fail-closed by default, fail-open only by explicit opt-in.

| Failure | Behavior |
|---|---|
| Redis down | Policy reads fall through to Postgres; SSE stops; Celery tasks queue and drain on recovery. No data lost. |
| Worker down | Intercepts continue. Plan evaluations queue up. |
| Model returns garbage, refuses, or errors | The task retries three times with backoff (1s, 2s, 4s). If every attempt fails, it opens a pending approval for the action and writes a `plan.evaluation_failed` row (decision `flagged`) to the audit chain. The drift score is unchanged, since there was no verdict. |
| Spine unreachable | The Claude Code hook blocks the tool call rather than allowing it. |
| No policy matches | Denied. There is no implicit allow. |
| Webhook receiver slow | 2s timeout, does not block other deliveries or the hot path. |

Nothing in Spine *requires* Redis to be up. If you add a dependency, preserve
that property.

---

## Scaling

The API and worker are stateless; state lives in Postgres and Redis. Scaling
out means running more containers of either. The pieces that would need
attention first under real load:

- `audit_events` grows unbounded — roughly 2 rows per agent tool call.
  Partitioning by month and archiving cold data is the obvious next step.
- The per-org advisory lock on audit writes serializes writes within an
  organization. Fine at current volumes, a bottleneck at very high per-org
  throughput.
- Policy evaluation is linear in the number of policies per org, from an
  in-memory cache. Fine for hundreds; would need indexing for thousands.

None of this has been exercised under production load.
