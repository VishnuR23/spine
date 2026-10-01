# API reference

Base URL is your Spine instance — `http://localhost:8000` for local development.
Interactive docs are served at `/docs` outside production.

The machine-readable contract is [`docs/openapi.json`](openapi.json),
regenerated with `make openapi`.

---

## Authentication

| Credential | Header | Used for |
|---|---|---|
| Admin key | `X-API-Key: <ADMIN_API_KEY>` | Platform operations — creating organizations and minting their first key |
| Org key | `X-Org-Key: spine_...` | Everything an agent does: intercept, sessions, policies, audit, webhooks |
| User JWT | `Authorization: Bearer <jwt>` | Dashboard users, scoped to their organization |

Org keys are shown once at creation and stored only as a SHA-256 hash. There is
no endpoint that returns an existing key.

Admin routes also accept an OIDC access token instead of the admin key if
`OIDC_JWKS_URL` and `OIDC_AUDIENCE` are configured; the token must carry the
role named by `OIDC_ADMIN_ROLE` (default `spine_admin`).

---

## Walkthrough

`make demo` does all of this for you. This is the same sequence by hand.

**Create an organization** (admin key):

```bash
export SPINE_ADMIN_KEY="change-me"

curl -sS -X POST "http://localhost:8000/v1/orgs" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $SPINE_ADMIN_KEY" \
  -d '{"name":"demo-org","plan":"starter"}'
```

**Mint an org key** — copy `raw_key` from the response, it is not shown again:

```bash
export ORG_ID="<org id from above>"

curl -sS -X POST "http://localhost:8000/v1/orgs/$ORG_ID/api-keys" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $SPINE_ADMIN_KEY" \
  -d '{"name":"local-dev"}'

export ORG_KEY="<raw_key from above>"
```

**Register an agent:**

```bash
curl -sS -X POST "http://localhost:8000/v1/agents/register" \
  -H "Content-Type: application/json" \
  -H "X-Org-Key: $ORG_KEY" \
  -d '{"name":"demo-agent","framework":"generic"}'

export AGENT_ID="<id from above>"
```

**Create a policy.** Nothing is allowed until one matches:

```bash
curl -sS -X POST "http://localhost:8000/v1/policies" \
  -H "Content-Type: application/json" \
  -H "X-Org-Key: $ORG_KEY" \
  -d '{
    "name": "Allow reads under /data",
    "rule_type": "action",
    "rule_config": {
      "effect": "allow",
      "action_types": ["read"],
      "target_resource_regex": "^/data/.*"
    }
  }'
```

**Intercept an action:**

```bash
curl -sS -X POST "http://localhost:8000/v1/intercept" \
  -H "Content-Type: application/json" \
  -H "X-Org-Key: $ORG_KEY" \
  -d "{
    \"agent_id\": \"$AGENT_ID\",
    \"action\": {
      \"action_type\": \"read\",
      \"target_resource\": \"/data/report.csv\",
      \"metadata\": {\"ticket\": \"INC-42\"}
    }
  }"
```

Change the target to `/etc/passwd` and it is denied — no policy matches, and
the default is deny.

**Read the audit log, and verify it:**

```bash
curl -sS "http://localhost:8000/v1/audit?limit=50" -H "X-Org-Key: $ORG_KEY"
curl -sS "http://localhost:8000/v1/audit/verify"  -H "X-Org-Key: $ORG_KEY"
```

---

## Policy rules

`rule_config` supports:

| Field | Values | Meaning |
|---|---|---|
| `effect` | `allow`, `deny`, `flag` | Default `deny`. `flag` withholds the action (`allowed: false`) and opens an approval ticket; once a human approves, a time-boxed grant lets the retry through. |
| `action_types` | list of strings | Which action types this rule covers. Omit to match any. |
| `target_resource_regex` | regex | Applied to `target_resource`. Omit to match any. |
| `time_window_utc` | `{"start":"09:00","end":"17:00"}` | UTC window; overnight ranges are supported. |

Policies are cached in Redis per organization for 300 seconds and invalidated
explicitly on write.

---

## Sessions and plan review

Open a session to enable plan-bound review. Full detail in
[PLAN_BOUND_MONITORING.md](PLAN_BOUND_MONITORING.md).

```bash
curl -sS -X POST "http://localhost:8000/v1/sessions" \
  -H "Content-Type: application/json" \
  -H "X-Org-Key: $ORG_KEY" \
  -d "{
    \"agent_id\": \"$AGENT_ID\",
    \"goal\": \"Summarize last week's support tickets\",
    \"constraints\": [\"read-only\", \"no external network calls\"],
    \"expected_resources\": [\"/data/tickets/*\"],
    \"success_criteria\": \"a summary written to /data/summary.md\"
  }"
```

Pass the returned `session_id` on subsequent intercepts. Without one, no model
is called.

---

## Operational headers

**Idempotency.** Pass `Idempotency-Key` on `/v1/intercept` to retry safely.
Same key and same request returns the cached response; same key with a
different request returns 409.

**Correlation.** `X-Request-ID` is echoed back as `request_id`. The intercept
body also accepts a structured `correlation` object.

---

## Webhooks

Register a URL with `POST /v1/webhooks` (the response includes `hmac_key` once).
Spine then POSTs on each decision, on plan drift, and when a plan review fails:

- `X-Spine-Event: spine.intercept`, `spine.plan.drift`, or `spine.plan.review_failed`
- `X-Spine-Signature: v1=<hex>` where the hex is
  `HMAC_SHA256(bytes.fromhex(hmac_key), raw_body_bytes)`

A webhook's `events` list filters what it receives; empty means everything.
`spine.plan.review_failed` is sent after every retry of a plan review fails
and the action has been handed to a human. Its body carries `session_id`,
`agent_id`, `audit_event_id`, `action`, `approval_id`, and `error_type` (an
exception class name). Subscribe to it with `spine.plan.review_failed`,
`plan.review_failed`, `decision:plan_review_failed`, or `decision:*`.

URLs are SSRF-validated when `BLOCK_WEBHOOK_PRIVATE_URLS` is on: private,
loopback, and link-local addresses are rejected, and production requires HTTPS.

---

## Live event stream

`GET /v1/audit/stream` is a Server-Sent Events endpoint carrying `connected`,
`audit`, `approval`, `plan_evaluation`, `plan_drift`, and `heartbeat` events.
Requires Redis. Behind the dashboard BFF it is `/api/spine/v1/audit/stream`
with cookie auth.

If you terminate TLS in front of it, disable proxy buffering on that path.
The API sets `X-Accel-Buffering: no`; Caddy passes SSE through unchanged.

---

## Approvals

Flagged actions and drift both open approval tickets, resolvable in the
dashboard or via `POST /v1/approvals?org_id=<uuid>`. Approving issues a
time-boxed grant (`APPROVAL_GRANT_TTL_SECONDS`, default one hour) so the agent
can retry the action.

---

## Metrics

`GET /metrics` exposes Prometheus metrics, including
`spine_intercept_duration_seconds` and `spine_monitor_evaluations_total`.
Gate it behind the admin key with `METRICS_REQUIRE_ADMIN_KEY=true`.
