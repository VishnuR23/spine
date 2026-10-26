# Threat model

Spine is a security control, so it should say precisely what it defends
against, what it assumes, and where its guarantees stop. This document is that
statement. It is written to be checked against the code, and every claim points
at the file that enforces it.

The other docs describe the mechanisms. This one describes the adversary.

---

## Assets

What an attacker would want, in the order Spine cares about them:

1. **The verdict path.** Getting an off-plan or forbidden action classified as
   allowed — by the policy engine or by the mandate reviewer.
2. **The audit chain.** Making `GET /v1/audit/verify` return `ok` for history
   that was altered, or preventing an action from being recorded at all.
3. **Human approval.** Obtaining a grant without a human actually approving, or
   stretching an approval past what was approved.
4. **Credentials.** Raw org keys, the JWT secret, the admin key.
5. **Tenant isolation.** Reading or writing another organization's data.
6. **The network position.** Using Spine's webhook dispatcher to reach
   addresses the attacker cannot reach directly.

---

## Trust boundaries

```
untrusted                          │ trusted
                                   │
 content the agent reads           │
 (files, tool output, web pages) ──┼──▶ worker agent ──▶ POST /v1/intercept
                                   │                         │
                                   │            policy engine (deterministic)
                                   │                         │
                                   │            audit log (hash-chained)
                                   │                         │
                                   │          mandate reviewer ◀── declared plan (operator)
                                   │                  │
                                   │            human approver
```

- **The worker agent is untrusted.** Spine assumes the agent can be fully
  compromised by content it reads. Every field the agent sends to
  `/v1/intercept` is treated as attacker-controlled, including `action_type`,
  `target_resource`, and `metadata`.
- **The declared plan is trusted.** It is created by the operator through
  `POST /v1/sessions` under an org key. Spine governs the agent against the
  plan; it does not audit the plan. A plan that authorizes damage authorizes
  damage.
- **The reviewer's input builder is the boundary.**
  `build_plan_user_message` in `spine/monitor/prompts/plan_alignment.py` is the
  only function that decides what the reviewer sees. Its signature is the
  security property.
- **The human approver is trusted** for the scope and duration of a grant
  (`spine/core/approval_grants.py`, default one hour).
- **Operators and admins are trusted.** Spine has no defense against the
  person holding the admin key.

---

## Attacker capabilities

Numbered so the defenses below can refer to them.

- **A1 — Indirect prompt injection into the agent.** The attacker places
  instructions in something the agent will read. The agent complies and
  attempts actions outside its plan. This is the primary threat and the reason
  the mandate reviewer exists.
- **A2 — Injection aimed at the reviewer.** The attacker's instructions are
  addressed not to the agent but to the monitor judging it, and the attacker
  tries to get them into the reviewer's context through any field the agent
  controls.
- **A3 — Verdict spoofing.** The attacker tries to make agent-controlled bytes
  parse as, or be mistaken for, a reviewer verdict or a reviewer instruction.
- **A4 — Drift dilution.** A compromised agent interleaves many clearly
  aligned actions with occasional off-plan ones to keep the session's drift
  score under the thresholds.
- **A5 — History poisoning.** The attacker tries to shape what the reviewer
  sees as prior context by influencing earlier actions in the session.
- **A6 — Audit tampering.** Someone with database access edits or deletes
  audit rows.
- **A7 — Replay and double-recording.** Network retries or deliberate replays
  of intercept requests.
- **A8 — Webhook SSRF.** A webhook URL pointing at internal infrastructure.
- **A9 — Credential recovery.** Any endpoint or log that could surface a raw
  org key or secret.
- **A10 — Cross-tenant access.** A request under one org's key touching
  another org's rows.

---

## Defenses, by attacker capability

| Capability | Defense | Enforced in | Tested by |
|---|---|---|---|
| A1 | Default-deny policy on every intercept; plan-bound review scores each action against the declared plan without reading what the agent read | `spine/core/policy_engine.py`, `spine/monitor/plan_engine.py` | `tests/test_policy_engine.py`, `tests/test_plan_engine.py`, `tests/test_e2e_plan_bound.py` |
| A2 | Structural isolation: the reviewer input builder has no parameter for file contents, tool output, prompts, or metadata; history entries are flattened to three string fields and everything else is dropped | `spine/monitor/prompts/plan_alignment.py` | `tests/test_plan_prompt_isolation.py`, `tests/test_reviewer_injection_corpus.py` |
| A3 | Reviewer input is a JSON document Spine serializes itself; agent-controlled strings can only appear as string *values* in fixed slots and cannot add keys, change structure, or escape their slot. The system prompt additionally instructs the reviewer not to follow instructions inside action data | `spine/monitor/prompts/plan_alignment.py` | `tests/test_reviewer_injection_corpus.py` (structure-invariance tests) |
| A4 | A single `divergent` verdict opens an approval regardless of the running score; the EMA (α = 0.3) means the score cannot be reset by volume alone, only decayed | `spine/monitor/plan_engine.py` | `tests/test_plan_engine.py` |
| A5 | History shown to the reviewer is built from Spine's own `plan_evaluations` rows joined to `action_type`/`target_resource`, never from agent-supplied context; capped at the last 40 entries | `spine/monitor/plan_engine.py::_load_recent_history` | `tests/test_plan_prompt_isolation.py` |
| A6 | Each audit row's hash covers its content and the previous row's hash; verification recomputes the chain and reports the first broken row | `spine/core/audit_logger.py`, `spine/core/audit_verify.py` | `examples/audit_tamper_detection.py` (runs in CI) |
| A7 | `Idempotency-Key`: same key and fingerprint returns the cached response; same key and different fingerprint returns 409 | `spine/core/idempotency.py` | `tests/test_intercept_api.py` |
| A8 | Webhook URLs rejected if private, loopback, link-local, internal, or non-HTTPS in production | `spine/core/url_validation.py` | `tests/test_security.py` |
| A9 | Org keys stored as SHA-256 only, shown once at mint; secrets stripped from metadata before logs, SSE, and prompts | `spine/api/routes/api_keys.py`, `spine/core/redaction.py` | `tests/test_auth.py`, `tests/test_security.py` |
| A10 | Every query filtered by `org_id` in application code | route modules under `spine/api/routes/` | `tests/test_members_api.py`, `tests/test_sessions_api.py` |

---

## Residual attack surface

This is the section that matters most, because a threat model that lists only
wins is marketing.

**The reviewer does see two agent-controlled strings.** `action_type` (capped
at 100 characters) and `target_resource` (capped at 2048) are declared by the
agent at intercept time and are passed to the reviewer, because the reviewer
cannot judge an action it cannot see. This is a real channel for A2 and A3.
What contains it:

- the strings land as JSON string values inside a structure Spine builds, so
  they cannot introduce keys, alter the schema, or masquerade as a separate
  message;
- length caps bound how much an attacker can say;
- the system prompt instructs the reviewer to treat every field as data.

The containment is tested (`tests/test_reviewer_injection_corpus.py`), but it
is containment, not absence. A sufficiently persuasive 2048-character
`target_resource` is a live research question, and it is the reason the
project invites adversarial testing of the reviewer. Prior work on automated
attack generation against agents (AgentVigil, arXiv:2505.05849) is the obvious
tool to point at this channel.

**Review is asynchronous.** The action that triggers a verdict has already
been allowed by policy. Enforcement lands on the session's next action. An
attacker who needs exactly one off-plan action gets it. `PLAN_BOUND_MONITORING.md`
describes the synchronous reviewer that would close this and is not built.

**Drift can be decayed, not reset.** A4 is mitigated, not eliminated: a
pattern of `drifted` (0.2–0.5) verdicts spaced between aligned actions can
stay under a 0.4 flag threshold. Lower thresholds trade this for more
interruptions.

**The reviewer does not read what was written.** An action the plan permits
(write to `/src/auth/login.ts`) is approved on its type and target. Its
contents are out of scope.

**Redaction is best-effort.** `spine/core/redaction.py` strips common secret
shapes from metadata. It is a safety net; the boundary is "don't put secrets
in metadata."

**Tenant isolation has no database backstop.** A route that forgets an
`org_id` filter is a cross-tenant bug with nothing beneath it.

**No rate limit on `/v1/intercept`.**

---

## Relation to published frameworks

Two recent lines of work describe what Spine is trying to be, and it is useful
to state the mapping explicitly.

*A Framework for Formalizing LLM Agent Security* (Siu et al., arXiv:2603.19469)
proposes four properties. In Spine's terms:

| Property | Where Spine enforces it | Where it does not |
|---|---|---|
| Task alignment — the agent pursues the authorized objective | Plan-bound review against the declared goal | Only for sessions that declare a plan |
| Action alignment — each action serves that objective | Per-action verdict; drift accumulation | Asynchronous; the triggering action is already allowed |
| Source authorization — commands come from authenticated sources | Org keys on every request; the plan comes from the operator, never from content the agent read | Nothing verifies who wrote the plan |
| Data isolation — information flows respect privilege boundaries | The reviewer input boundary; redaction | The two agent-declared strings above |

*Progent* (Shi et al., arXiv:2504.11703) enforces least privilege with
symbolic per-tool-call policies and treats every policy change as either a
narrowing (automatic) or an expansion (requires approval). Spine's
deterministic layer is a coarser version of the same posture — default-deny,
admin-authored rules — and its approval grants are time-boxed expansions that
require a human. Spine does not derive policies from the task and does not
verify policy updates; the mandate reviewer covers intent with a model call
instead. The two approaches are complementary, and adopting Progent-style
policy synthesis for the deterministic layer is a natural extension.

---

## What to report

If you can do any of the following, it is a vulnerability and we want to know
(see `SECURITY.md`):

- get an action allowed with no matching policy;
- get agent-controlled bytes into the reviewer's input through anything other
  than `action_type` and `target_resource`;
- get a `target_resource` payload to change the structure of the reviewer's
  input, rather than sit inside it as a string value;
- make `GET /v1/audit/verify` return `ok` on an altered log;
- obtain a grant without a human approving;
- reach another org's data or an internal address through Spine.
