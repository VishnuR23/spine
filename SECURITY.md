# Security Policy

## Reporting a vulnerability

Please report security issues privately, not as public GitHub issues.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, or email **vishnu@spinelayer.com**.

Please include what you were able to do, the steps to reproduce it, and the
version or commit you tested. If you have a proof of concept, include it.

This is a small project, currently maintained by one person. Expect an
acknowledgement within a few days. There is no bounty program.

## Scope

Spine is a security control, so the findings that matter most are the ones
that break a claim it makes:

- **Bypassing the intercept path** — getting an action allowed without a
  matching policy, or defeating default-deny.
- **Forging or silently editing the audit chain** — making `GET /v1/audit/verify`
  return `ok` for a log that was altered.
- **Reaching the plan reviewer's input** — getting agent-controlled content
  (file contents, tool output, metadata, prompts) into the reviewer's context.
  See `examples/injection_isolation.py` for what is being claimed.
- **Cross-tenant access** — reading or writing another organization's data.
- **Credential exposure** — recovering a raw org key, a JWT secret, or the
  admin key through any endpoint.
- **SSRF via webhooks** — getting Spine to call an internal address in a
  production configuration.

## Known limitations, by design

These are documented rather than fixed. Reporting them is welcome, but they
are not surprises:

- **The reviewer is asynchronous.** It scores an action after it has been
  allowed. Enforcement is against the session's subsequent actions.
- **Multi-tenancy is enforced in application code**, not Postgres row-level
  security. A missing `org_id` filter in a new route is a real bug — please
  report it — but there is no database-level backstop.
- **Redaction is a safety net, not a boundary.** `spine/core/redaction.py`
  strips common secret shapes from metadata; it will not catch everything.
  Do not put secrets in intercept metadata.
- **The plan reviewer depends on the declared plan being honest.** Spine
  governs the agent, not the operator who wrote the plan.
- **No rate limit on `/v1/intercept`.** Login is rate-limited; the intercept
  path is not.

## Running Spine safely

- Set `JWT_SECRET` and `ADMIN_API_KEY` to strong, distinct values. In
  production the API refuses to start with defaults, or with a `JWT_SECRET`
  shorter than 32 bytes. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
  generates a suitable one.
- Keep `BLOCK_WEBHOOK_PRIVATE_URLS=true`.
- Serve behind TLS. The included Caddy configuration does this.
- Org keys are shown once and stored only as a SHA-256 hash. If one leaks,
  revoke and mint a new one — there is no endpoint that returns an existing
  key, by design.
