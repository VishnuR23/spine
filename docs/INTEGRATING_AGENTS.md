# Connecting autonomous agents to Spine

Spine is **middleware**: your agent must **call Spine before** doing anything sensitive. Spine does not silently attach to your runtime — the agent (or a plugin / sidecar) has to ask first.

```
BEFORE side effect  →  POST /v1/intercept  →  if allowed, proceed; else stop
```

Side effects include: placing, amending, or cancelling orders; reading licensed market data; reading/writing files, shell commands, HTTP calls, database queries, sending messages.

**Trading systems:** use the C++ pre-trade gate in [`sdks/cpp`](../sdks/cpp/). It sizes each order, picks the action type (`order.place`, `.large`, `.block`, `order.amend*`, `order.cancel`), enforces a hard latency budget, and fails closed — except for cancels, which go through if Spine cannot answer. The rest of this page covers other agent runtimes.

---

## Credentials (all integrations)

| Value | Where to get it |
|-------|-----------------|
| **Base URL** | Your Spine endpoint, e.g. `https://api.demo.spinelayer.com` |
| **Org key** | Dashboard → Settings → API keys → **Mint** → `spine_...` (shown once) |
| **Agent ID** | Dashboard → Agents → register agent → copy UUID |

HTTP header: `X-Org-Key: spine_...`

---

## Pattern A — Claude Code (recommended)

Spine ships a Claude Code `PreToolUse` hook that intercepts every tool call (Read, Write, Edit, Bash, WebFetch, MCP, …) plus slash commands for declaring a plan-bound session per task.

```bash
cd integrations/claude-code-spine && ./install.sh
```

Then edit `~/.spine/config.json` with your `base_url`, `org_key`, and `agent_id`. In any Claude Code session:

```
/spine-session-start "Refactor the auth module to use JWT instead of session cookies"
```

Every subsequent tool call is intercepted, evaluated by the plan-bound reviewer, and contributes to the session's drift score. Off-plan actions are flagged or hard-blocked depending on your thresholds.

Full guide: **[integrations/claude-code-spine/README.md](../integrations/claude-code-spine/README.md)**.

---

## Pattern B — Python agent / LangGraph / custom tools

Use the Python SDK (`sdks/python`) or HTTP directly.

**Decorator** (`sdks/python/examples/langgraph_tool.py`):

```python
from spine_sdk import SpineClient, guard_tool

client = SpineClient(base_url="...", org_key="spine_...", http_client=httpx.Client())


@guard_tool(client, agent_id=AGENT_ID, action_type="read", target_resource_for_action=lambda path, **_: path)
def read_file(path: str): ...
```

**Inline check**:

```python
res = client.intercept(agent_id=aid, action_type="bash", target_resource=command)
if not res.json.get("allowed"):
    raise RuntimeError(res.json.get("reason", "blocked"))
os.system(command)  # only if allowed
```

---

## Pattern C — TypeScript / Node agents

Use `sdks/ts` — `SpineClient.intercept(...)`. Same shape as Python.

---

## Pattern D — Manual / CI testing

Dashboard **Intercept** page or curl:

```bash
curl -X POST "$SPINE_URL/v1/intercept" \
  -H "Content-Type: application/json" \
  -H "X-Org-Key: $SPINE_ORG_KEY" \
  -d '{"agent_id":"'"$AGENT_ID"'","action":{"action_type":"read","target_resource":"/private/x"}}'
```

---

## Policy tips

- Spine is **default-deny**. No matching policy → **blocked**.
- Match the agent's tool name in `action_types` (e.g. `read`, `write`, `exec`).
- Match resource patterns in `target_resource_regex` (anchored: `^/src/auth/.*`).
- Use **flag** effect for human review (approvals), **deny** for hard block.

Plan-bound monitoring is what makes Spine differentiated: see **[PLAN_BOUND_MONITORING.md](./PLAN_BOUND_MONITORING.md)**.

---

## Comparison

| Agent type | Integration | Blocks before action? |
|------------|-------------|------------------------|
| Claude Code | `integrations/claude-code-spine` hook | Yes |
| Python custom | SDK / decorator | Yes, if you call intercept |
| No integration | — | **No** — Spine never sees it |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Agent does anything | Hook not installed or not enabled |
| Always blocked | No matching allow policy — add one under Policies (Spine is default-deny) |
| Blocked when Spine down | `failClosed: true` — expected |
| 403 on org | Wrong or revoked `spine_...` key |
| Path not matched | Check `target_resource` in audit log; tune regex |

The audit log shows every intercept attempt — use it to debug policy matching.
