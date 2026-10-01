# Plan-bound monitoring

A policy engine can tell you an action is forbidden. It cannot tell you an
agent has quietly stopped doing what it was asked to do, because each
individual step is permitted. Plan-bound monitoring is Spine's answer to that:
the agent declares a plan up front, and every action is scored against it.

The design constraint that shapes everything else: **the reviewer must not read
anything the agent could have influenced.**

---

## Why that constraint

The straightforward way to build an LLM monitor is to show it the agent's
context — the prompts, the files it read, the tool results — and ask whether
anything looks wrong. That works until someone writes instructions into
something the agent will read. At that point the attacker is addressing your
monitor directly, and the monitor's judgment is exactly as trustworthy as the
content it was fed.

Filtering is the usual patch: scan inputs for injection patterns before the
monitor sees them. It is a detection arms race, and the defender has to win
every round.

Spine takes the other option. The reviewer is never shown agent-influenced
content, so there is no channel to attack. What it sees instead is a
description of the action — its type and target — which is enough to judge
alignment without ever rendering attacker-controlled text into the prompt.

---

## The mechanism

**1. Declare a plan.** Before the agent starts work:

```http
POST /v1/sessions
X-Org-Key: spine_...

{
  "agent_id": "…",
  "goal": "Refactor the authentication module to use JWT instead of session cookies",
  "constraints": ["only touch /src/auth/**", "no schema changes", "no external network calls"],
  "expected_resources": ["/src/auth/*.ts", "/tests/auth/*.test.ts"],
  "success_criteria": "all auth tests pass, no other tests break"
}
```

Returns a `session_id`.

**2. Carry the session.** Every `POST /v1/intercept` includes `session_id`.
The audit row is tagged with it. Without a session, no review happens and no
model is called.

**3. Review, off the hot path.** A Celery task calls the model with exactly
three things:

- the declared plan,
- the current action's `action_type` and `target_resource`,
- prior verdicts in this session, flattened to
  `(action_type, target_resource, alignment)` triples.

It does not receive file contents, tool outputs, the agent's prompts or
reasoning, or intercept metadata. Not by filtering — `build_plan_user_message`
in [`spine/monitor/prompts/plan_alignment.py`](../spine/monitor/prompts/plan_alignment.py)
has no parameter that accepts them. The function signature *is* the security
boundary, which is what makes the property auditable: you can read it in under
a minute and see there is no path in.

`tests/test_plan_prompt_isolation.py` asserts this against adversarial inputs.
`examples/injection_isolation.py` demonstrates it interactively.

**4. Verdict.** One of `aligned`, `drifted`, or `divergent`, each carrying a
`drift_contribution` between 0 and 1.

**5. Drift score.** Per session, an exponential moving average, so recent
behavior dominates but a pattern of small deviations still accumulates:

```
drift_new = 0.3 × drift_contribution + 0.7 × drift_old
```

**6. Enforcement.**

- **Flag** — a `divergent` verdict, or drift crossing `PLAN_DRIFT_FLAG_THRESHOLD`
  (default 0.4), opens an approval ticket and fires a `spine.plan.drift` webhook.
- **Block** — drift crossing `PLAN_DRIFT_BLOCK_THRESHOLD` (default 0.6) causes
  every subsequent intercept in that session to be refused synchronously,
  before policy evaluation runs. The agent needs a new session to proceed.
- **Review failed** — if every retry of the model call fails, the action is
  handed to a human instead: an approval ticket opens, a
  `plan.evaluation_failed` row lands in the audit chain, and a
  `spine.plan.review_failed` webhook fires. Drift is unchanged.

Approving a flagged action issues a time-boxed grant (default 1 hour) so the
agent can retry without the whole session being re-litigated.

---

## What it catches that a policy engine does not

**Injection-induced drift.** A poisoned file tells the agent to back up `.env`
first. The agent complies. The plan says refactor auth; reading
`/etc/secrets/.env` does not fit, and the reviewer says so — without ever
seeing the poisoned file.

**Scope creep.** Plan says summarize last week's email. The agent starts
reading Slack, then hitting external APIs. No single read is forbidden.

**Cumulative damage.** Plan says clean up `/tmp`. The agent issues five
thousand deletes across the filesystem. Each `rm` is individually legal.

**Goal subversion.** Plan says review this pull request. The agent's actions
trend toward approving and merging it. Each step looks reasonable; the
direction does not.

**Cascading multi-agent failure.** Agent A produces a wrong claim, B builds on
it, C acts on B. Every action is legal under static policy, and nothing in the
plan supports the conclusion.

---

## What it does not catch

- **Anything inside an allowed action.** If the plan permits writing to
  `/src/auth/`, the reviewer sees "write to /src/auth/login.ts" and approves.
  It does not read what was written.
- **The action that triggers the verdict.** Review is asynchronous;
  enforcement lands on the session's next action.
- **A dishonest plan.** Spine governs the agent against the declared plan. A
  plan that authorizes the damage authorizes the damage.
- **Agents that never open a session.** Review is opt-in per session by
  design — it costs a model call.

---

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for the reviewer |
| `MONITOR_MODEL` | `claude-sonnet-5-5` | Reviewer model |
| `PLAN_DRIFT_FLAG_THRESHOLD` | `0.4` | Opens an approval ticket |
| `PLAN_DRIFT_BLOCK_THRESHOLD` | `0.6` | Hard-blocks the rest of the session |
| `PLAN_EVAL_HISTORY_WINDOW` | `10` | Prior verdicts shown to the reviewer |
| `MONITOR_SKIP_IF_POLICY_BLOCKED` | `true` | Skip review for actions policy already blocked |
| `APPROVAL_GRANT_TTL_SECONDS` | `3600` | Grant lifetime after a human approves |

Thresholds are the main tuning knob. Lower them and you review more and
interrupt more; raise them and drift has to be more pronounced before anyone
is asked. The defaults are a starting point, not a calibrated result.

---

## Where it lives

| Concern | File |
|---|---|
| Reviewer input builder (the boundary) | `spine/monitor/prompts/plan_alignment.py` |
| Reviewer engine, worker-side | `spine/monitor/plan_engine.py` |
| Celery task | `spine/worker/tasks.py` |
| Sessions CRUD | `spine/api/routes/sessions.py` |
| Synchronous drift gate | `spine/core/intercept_service.py` |
| Tables | `migrations/versions/0010_plan_bound_monitoring.py` |
| Isolation test | `tests/test_plan_prompt_isolation.py` |

---

## Verifying it end to end

```bash
bash tools/smoke_plan_bound.sh
```

Boots a server against a fresh SQLite database, runs the full session
lifecycle over HTTP, drives the drift gate, and invokes the Claude Code hook
as a subprocess. Takes about 30 seconds and needs no API key.
