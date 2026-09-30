# Claude Code — start here

You are working on **Spine**: policy + monitoring middleware that sits between
autonomous AI agents and the systems they act on. Read this entire file before
making any change. It is the operating manual, not just an orientation.

For deeper context: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how the
pieces fit), [docs/PLAN_BOUND_MONITORING.md](docs/PLAN_BOUND_MONITORING.md)
(the plan reviewer), [docs/INTEGRATING_AGENTS.md](docs/INTEGRATING_AGENTS.md)
(how agents connect), [docs/API.md](docs/API.md) (endpoint reference).

---

## What Spine is, in one paragraph

Every action an agent wants to take goes through `POST /v1/intercept` first.
A synchronous regex-based policy engine returns allow/block/flag in under 50 ms
at p99. The decision and full action context land in a hash-chained audit log.
When the intercept carries a `session_id`, a context-isolated plan reviewer runs
asynchronously on the Celery worker, scoring the action against the agent's
declared plan; sufficient drift opens a human approval ticket or hard-blocks
future intercepts. A reference deployment runs on a single VPS behind Caddy —
see `deploy/vps/`. Note that the dashboard and the API are separate hosts:
agents must call the API host, not the dashboard host.

---

## Architecture map

```
spine/
├── api/routes/              # FastAPI routers — one file per resource
│   ├── intercept.py         # POST /v1/intercept (the hot path)
│   ├── sessions.py          # plan-bound sessions CRUD
│   └── ...                  # policies, audit, approvals, webhooks, etc.
├── core/                    # business logic, no HTTP
│   ├── intercept_service.py # the intercept pipeline
│   ├── policy_engine.py     # pure regex + time-window matcher
│   ├── policy_cache.py      # Redis-backed cache for the hot path
│   ├── audit_logger.py      # async hash-chained writes
│   ├── sync_audit_logger.py # sync variant for the Celery worker
│   ├── audit_verify.py      # GET /v1/audit/verify
│   ├── approval_grants.py   # 1-hour TTL grants after human approve
│   ├── webhook_dispatch.py  # HMAC-signed POSTs to customer URLs
│   ├── event_bus.py         # Redis pub/sub for SSE
│   ├── redaction.py         # strip secrets before logs/SSE/prompts
│   └── idempotency.py       # Idempotency-Key replay protection
├── monitor/
│   ├── plan_engine.py       # plan-bound reviewer (worker-side)
│   └── prompts/             # plan_alignment — the isolation boundary
├── models/                  # SQLAlchemy ORM (one class per file)
├── schemas/                 # Pydantic request/response models
├── auth/                    # JWT, OIDC, bcrypt
├── worker/                  # Celery app + tasks (async plan reviewer)
├── observability/           # Prometheus histograms + OTLP
└── db/                      # async + sync engines, GUID type

spine-dashboard/             # React + Vite + Express BFF
integrations/claude-code-spine/  # PreToolUse hook + slash commands
deploy/vps/                  # production Docker Compose + Caddyfile
migrations/versions/         # Alembic (0001 → 0013, applies on api startup)
tools/                       # seed_demo.py, create_user.py, smoke_plan_bound.sh
```

---

## Safety invariants — never violate these

These are not opinions. Violating them ships bugs that compromise the product's
core value claim. If a change would touch any of them, stop and surface it
before editing.

### 1. The hash-chained audit log must remain verifiable forever

Every audit row's `event_hash` is computed from its content plus the previous
row's hash. `GET /v1/audit/verify` recomputes the chain on demand. **The
canonical JSON formula in `audit_logger.py`, `sync_audit_logger.py`, and
`audit_verify.py` must match exactly — character for character.** If you add a
field that participates in the hash, add it in all three places identically and
gate inclusion on non-null so historical rows still verify.

The reason `session_id` is included only `if session_id is not None` is that
this preserves the property: pre-migration rows recompute identically (no
session_id key in their canonical JSON) and post-migration rows that happened
to lack a session_id also recompute identically. **Never add a field to the
canonical JSON unconditionally.** Doing so would invalidate every historical
row's hash and break the compliance claim.

### 2. The plan reviewer must never see the worker agent's attack surface

`spine/monitor/plan_engine.py` calls `build_plan_user_message` in
`spine/monitor/prompts/plan_alignment.py`. The function signature is
deliberately narrow: it accepts only the declared plan (Spine-owned),
the current action's `action_type` and `target_resource` (agent-declared but
minimal and structured), and a summarized history of prior plan_evaluations
reduced to `(action_type, target_resource, alignment)` triples.

**Never extend that function to accept audit metadata, file contents, tool
outputs, worker prompts, or anything else that originated in the worker
agent's context window.** The structural isolation is the entire defense
against prompt injection. The test `tests/test_plan_prompt_isolation.py`
exists specifically to catch this and must stay green.

The reviewer is a *separate Claude call*. Its tool set (if you ever expand it
beyond the current zero tools) must be a strict subset of the
`get_session_plan` / `get_session_action_history` pattern — nothing that could
surface worker context.

### 3. Default-deny is the production posture

`policy_engine.decide` returns `("blocked", False, "No matching policy")` when
no rule matches. Don't change that default. Don't introduce a global fallback
allow. The only legitimate way to allow more behavior is policies in the
`policies` table, which an admin explicitly creates.

### 4. Idempotency on intercept writes must be preserved

`POST /v1/intercept` accepts an `Idempotency-Key`
header. The same key + same request fingerprint returns the cached response;
the same key + different fingerprint returns 409. This exists so the agent
can safely retry a request that timed out without double-recording the
audit event or double-firing the webhook. The logic is in
`spine/core/idempotency.py`. **Don't add side effects to intercept that aren't
covered by the idempotency cache.**

### 5. Webhook URLs must be SSRF-validated in production

`spine/core/url_validation.py` rejects private IPs, loopback, link-local,
internal hostnames, and (in production) non-HTTPS. `BLOCK_WEBHOOK_PRIVATE_URLS`
in settings gates this. **Don't disable the validator without a compelling
reason and never in production.**

### 6. Org keys are sha256-hashed at rest; raw keys are shown once

When a key is minted (`spine/api/routes/api_keys.py` or `orgs.py`), the raw
`spine_…` string is returned in the response body exactly once. The DB stores
only the hash. **Don't add an endpoint that returns existing raw keys.** If a
customer loses their key, they revoke and mint a new one.

### 7. Secrets must never enter intercept metadata

The intercept metadata field is stored verbatim in the audit chain and is
forwarded (redacted) to the plan reviewer and SSE. `spine/core/redaction.py` strips
common patterns (emails, bearer tokens, JWTs, long hex strings, SSNs, credit
cards) and caps size at 8 KB / 64 keys. **The redaction layer is a safety
net, not the primary defense — clients should never put secrets in
metadata in the first place.** If you find a code path that's tempted to,
fix the client, don't loosen the redactor.

---

## Caching guidelines

### Redis policy cache (`spine/core/policy_cache.py`)

The intercept hot path reads policies from Redis, not Postgres, on every
call. The cache is per-org, TTL 300 seconds, key `spine:policies:cache:<org_id>`.

**Invalidation is explicit.** Any code path that creates, updates, or
deactivates a policy MUST call `invalidate(org_id)` after the DB commit. The
TTL exists only as a safety net. The routes that already do this:

- `POST /v1/policies` → `create_policy`
- `PUT /v1/policies/{id}` → `update_policy`
- `DELETE /v1/policies/{id}` → `deactivate_policy`

**If you add a new route or sync code path that mutates the `policies` table,
add `await invalidate_policy_cache(org_id)` after the commit.** Forgetting
this leaves the cache stale for up to 5 minutes and ships bugs that look like
"my new policy isn't taking effect."

For Celery worker code that needs to invalidate, use `invalidate_sync()`.

### Redis is also Celery broker + SSE pub/sub

The same Redis instance does three jobs: policy cache, Celery task queue,
and SSE pub/sub. **Don't add a fourth use without thinking about it.** Each
job has a key namespace prefix (`spine:policies:*`, Celery's own keys,
`spine:events:*`). Keep namespaces disjoint.

### When Redis is down

Every cache + queue + pub/sub path is wrapped to fail-safe. Policy reads fall
through to Postgres. Webhook delivery still works (it uses httpx, not Redis).
SSE stops working (the dashboard shows the live indicator going grey). Celery
tasks queue up; when Redis comes back, they drain. **The system gets slower
and loses live updates, but no permanent data is lost.** Preserve that
property — never make any feature *require* Redis to be up.

### httpx async client is module-level

`spine/core/webhook_dispatch.py` reuses one `httpx.AsyncClient` across all
calls via `_get_async_client()`. This keeps the connection pool / TLS state
warm. **Don't create per-call clients in the hot path.** Constructing one
adds ~30 ms.

---

## Rate limiting

### What exists

- **Login rate limit**: `LOGIN_RATE_LIMIT_PER_MINUTE` (default 20) per client IP
  on `POST /v1/auth/login`. Enforced by `spine/api/middleware/security.py`.
  Brute-force protection for dashboard credentials.

### What does not exist

- **No global rate limit on `/v1/intercept`**. Agents may call it at any
  frequency. If you need per-org rate limiting for billing or abuse control,
  add it as a new middleware that reads org_id from the auth dep and counts
  in Redis — but do not make it block the hot path if Redis is down.

- **No rate limit on plan-reviewer calls**. The reviewer makes one Claude call
  per evaluation (only for intercepts that carry a `session_id`), bounded by
  Anthropic's own per-key rate limits. If a customer's reviewer traffic exceeds
  Anthropic limits, evaluations back up in the Celery queue; intercepts continue
  normally.

- **No rate limit on webhook delivery**. Each event fans out to all matching
  registered webhooks in parallel. If a webhook receiver is slow, the
  `WEBHOOK_TIMEOUT_SECONDS` (default 2.0s) keeps it from blocking other
  deliveries.

### Rules of thumb when adding new endpoints

- Read-only endpoints (`GET`) — no rate limit needed beyond what the auth
  layer enforces (admin key vs org key vs JWT).
- Write endpoints that touch the policy/audit spine — these should be admin-
  gated (`OrgAdminDep`) which is enough; admin actions are low-volume.
- Anything that triggers LLM calls — bound it. The plan reviewer
  (`plan_engine.evaluate_plan_alignment`) makes a single bounded Claude call per
  evaluation (no tool-use loop) and runs only on the Celery worker.

---

## Scale & performance guidelines

### Latency budget on the hot path

`POST /v1/intercept` targets **p99 < 50 ms** end-to-end. Real p50/p95/p99 come
from the `spine_intercept_duration_seconds` histogram on
`GET /metrics`. If you change anything in
`spine/core/intercept_service.py` or `spine/api/routes/intercept.py`, check
locally that the median doesn't regress.

### What's allowed on the hot path

- Reading the Redis policy cache. Fast.
- Reading the `agents` table by primary key. Fast.
- Writing one row to `audit_events` with a Postgres advisory lock per-org.
- Optionally reading the `sessions` table by primary key (if session_id given).
- Firing a Celery `.delay()` (puts a task in Redis, returns immediately).

### What is NOT allowed on the hot path

- LLM calls. Never. The plan reviewer runs on the worker, not on the API
  process.
- HTTP calls to external services beyond the fire-and-forget webhook dispatch
  (which is itself async).
- Reading large tables without an index hit.
- Anything that scans `audit_events` — that table grows unbounded and you'll
  hit a wall as customers add traffic.

### Async = Celery, not threads

All long-running work (the plan-reviewer LLM call)
goes through Celery (`spine/worker/tasks.py`). The API process never spawns
background threads. Celery + Redis means horizontal scaling is just "add more
worker containers"; threads in the API process would not.

### Single droplet today, horizontal-ready tomorrow

Production runs one droplet because the customer count justifies it. The
architecture supports horizontal scaling without code changes: the API is
stateless (session state is in cookies + JWT, not in-memory), the worker is
stateless (state is in Postgres + Redis), Postgres + Redis can both go to
managed services. **Don't add in-memory state to the API.** If you find
yourself wanting a singleton dict or a process-local cache, put it in Redis
instead.

### Audit log growth

`audit_events` grows by one row per intercept plus one per plan evaluation
(only for intercepts that carry a `session_id`). At a customer with 10k tool
calls/day across 10 agents, that's ~20k rows/day. The table is indexed on (org_id, timestamp)
and (org_id, sequence) — the existing queries scale fine. The eventual play
is to partition by month and cold-archive to S3 after 90 days. **Don't add
queries that scan the table without an org_id + time bound.**

---

## Multi-tenancy invariants

Every table that contains user data has an `org_id` column. Every query that
reads or writes user data must filter on `org_id`. The auth layer
(`spine/api/deps.py`) hands you an authenticated `org_id`; use it as the
filter in every DB call.

There is no row-level security enforcement in Postgres today. The defense is
purely in application code. **If you write a new route, the first thing it
does after auth should be filter by `org_id`.** Tests
(`tests/test_e2e_plan_bound.py::test_cross_org_session_isolation`) check this
for sessions; add equivalent tests for new resources.

---

## Common commands

Most of these are wrapped by the Makefile — `make demo`, `make verify`,
`make test`, `make smoke`, `make logs`, `make clean`, `make openapi`. Run
`make` with no arguments for the list. The raw commands:

```bash
# Local dev (from repo root)
docker compose up --build
docker compose logs -f api worker        # tail logs
docker compose stop                      # pause, keep data
docker compose down -v                   # DESTROY data (use carefully)

# Production (on droplet)
cd deploy/vps && docker compose up -d --build

# Backend tests (must run from repo root)
pytest -v                                # full suite (67 tests as of writing)
pytest tests/test_plan_engine.py -v      # one file
pytest -k plan                           # any test whose name contains "plan"

# End-to-end smoke against a fresh SQLite (~30 sec, no Anthropic needed)
bash tools/smoke_plan_bound.sh

# Claude Code hook tests (separate dir)
cd integrations/claude-code-spine && python3 -m pytest tests/ -v

# Dashboard
cd spine-dashboard
npx tsc --noEmit                         # typecheck
npx vite build --outDir /tmp/out         # production build
npm run dev                              # local dev server (with HMR)

# Seeding (run inside the api container)
docker compose exec api python tools/quickstart.py   # org + agent + policies + login
docker compose exec api python tools/seed_demo.py    # minimal: org + agent + one policy
docker compose exec api python tools/create_user.py \
  --email you@local.dev --name "You" --org-id "$ORG_ID" \
  --role admin --password "spine12345"

# Migrations (auto-apply on api container start; manual:)
docker compose exec api alembic upgrade head
docker compose exec api alembic current
docker compose exec api alembic downgrade -1

# Direct DB inspection
docker compose exec db psql -U spine -d spine -c "SELECT ..."

# Regenerate OpenAPI export after route changes
PYTHONPATH=. DATABASE_URL="sqlite+aiosqlite:///:memory:" \
  ADMIN_API_KEY=x JWT_SECRET=x python3 tools/export_openapi.py
```

---

## Settings cheat sheet

Read `spine/config/settings.py` for the source of truth. Notable env vars:

```
# Identity / secrets
ADMIN_API_KEY              # platform admin (creates orgs); never weak in prod
JWT_SECRET                 # signs dashboard user tokens; required in prod
SESSION_SECRET             # BFF cookie signing (dashboard-side)
ANTHROPIC_API_KEY          # required for the plan reviewer LLM
MONITOR_MODEL              # plan reviewer model (default claude-sonnet-5-5)

# Toggles
SSE_ENABLED=true           # live dashboard updates
BLOCK_WEBHOOK_PRIVATE_URLS=true  # SSRF guard

# Thresholds
PLAN_DRIFT_FLAG_THRESHOLD=0.4    # opens approval ticket
PLAN_DRIFT_BLOCK_THRESHOLD=0.6   # synchronously hard-blocks future intercepts
PLAN_EVAL_HISTORY_WINDOW=10      # how many prior verdicts the reviewer sees
MONITOR_SKIP_IF_POLICY_BLOCKED=true # reviewer skips events the policy engine already blocked
APPROVAL_GRANT_TTL_SECONDS=3600  # 1 hour after human approve

# Limits
METADATA_MAX_BYTES=8192
REASONING_API_MAX_CHARS=500
LOGIN_RATE_LIMIT_PER_MINUTE=20
WEBHOOK_TIMEOUT_SECONDS=2.0
```

---

## Things that have caused bugs before — don't repeat

1. **Putting `[object Object]` on screen.** Some API responses (FastAPI 422
   validation errors) return `detail` as a list of dicts, not a string. The
   dashboard's auth client now handles this via `toMsg(v)` —
   `spine-dashboard/src/api/client.ts`. Any new error-display code path
   should coerce non-string values to JSON strings before rendering.

2. **Updating `.env` while the stack is already running.** The API
   container reads env vars at process start. After editing `.env`, you must
   `docker compose up -d --force-recreate` to pick up changes. Otherwise the
   running API still has the old values and you get mysterious auth failures.

3. **Hash-chain tests failing after schema changes.** If you add a field that
   participates in the audit hash, you must add it identically to the writer
   (`audit_logger.py` + `sync_audit_logger.py`) and the verifier
   (`audit_verify.py`). Run `tests/test_intercept_with_session.py::test_audit_chain_verifies_with_and_without_session_id` to catch this.

4. **Claude Code is the only first-party integration.** SDKs for Python and
   TypeScript exist under `sdks/`, but `integrations/claude-code-spine/` is
   the only one with a hook and tests.

5. **`tools/quickstart.py`, `seed_demo.py`, and `create_user.py` need to run
   inside the api container.** They use httpx and the project's deps; running
   them on the host requires installing dependencies separately. Use
   `docker compose exec api python tools/<script>.py ...`, or just `make demo`.

6. **`create_user.py` does NOT update passwords for existing users.** It adds
   memberships. If you need to reset a password, delete the user row or
   update `password_hash` directly via SQL.

7. **Emails ending in `.test`, `.example`, `.invalid`, `.localhost` are
   rejected by Pydantic's `EmailStr` at the `POST /v1/auth/login` endpoint.**
   Use a domain like `.dev` or `.com` for test users. The `create_user.py`
   script doesn't validate but the login endpoint does.

---

## Test surface (run before any PR)

| Suite | Command | Count (current) |
|---|---|---|
| Backend | `pytest -v` | 67 |
| Claude Code hook | `cd integrations/claude-code-spine && python3 -m pytest tests/` | 14 |
| Full E2E smoke | `bash tools/smoke_plan_bound.sh` | One run, ~30s |
| Dashboard typecheck | `cd spine-dashboard && npx tsc --noEmit` | Should be clean |
| Dashboard build | `cd spine-dashboard && npx vite build --outDir /tmp/out` | Should succeed |

All five must pass before any change ships. The smoke test exercises the
full intercept → audit chain → drift gate → hook subprocess path against
a real running uvicorn; it's the best single signal that nothing is broken
end-to-end.

---

## When you don't know which way to go

The repo follows two design principles consistently. When in doubt, follow
them.

**Zone-based resolution.** Not all parts of the system deserve equal
scrutiny. The "spine" (audit log, intercept service, plan reviewer, policy
engine) is treated with paranoia — every field reviewed, every test
exhaustive. The periphery (dashboard polish, internal tooling, UI strings)
moves fast. **Know which zone you're in before you decide how careful to be.**

**Fail-closed by default, fail-open by explicit opt-in.** Every integration
defaults to the safer behavior when something's wrong. The Claude Code hook
fails closed if Spine is unreachable. The policy cache falls through to
Postgres if Redis is down. One known exception: if the plan reviewer's model
call keeps failing (garbage, refusal, API error), the Celery task gives up
after three retries and that action is left unreviewed — see "Failure
behavior" in docs/ARCHITECTURE.md. **Preserve this discipline.** When you
add a new external dependency or async path, the default behavior on failure
must be the one that doesn't compromise the security claim.

---

## What "shipped" means in this repo

A feature is considered shipped when all of the following are true:
- The backend has a route, a test, and the test passes.
- The dashboard has a surface that an admin (not a dev) can use.
- The relevant docs (`docs/*.md`, `README.md`) mention it.
- The OpenAPI export is regenerated.
- The full test suite + smoke test pass.

Don't merge code that ticks fewer than all five. The product is sold as
production infrastructure; the bar for "done" is what an enterprise customer
would consider done, not what a prototype would.
