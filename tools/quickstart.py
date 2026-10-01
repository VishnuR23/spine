"""One-command demo setup: a trading desk.

Creates an org, an API key, a trading agent, the finance policy pack
(restricted list, notional bands with four-eyes approval, entitlements,
always-allowed cancels), the agent's declared trading mandate, and a
dashboard login. Prints the credentials and the exports the C++ pre-trade
gate needs.

Run it inside the API container (that is what `make demo` does):

    docker compose exec api python tools/quickstart.py

Safe to re-run — it creates a fresh org each time.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.create_user import create_user  # noqa: E402

BASE_URL = os.getenv("SPINE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_KEY = os.getenv("ADMIN_API_KEY", "change-me")
DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://spine:spine@db:5432/spine")

DEMO_EMAIL = os.getenv("SPINE_DEMO_EMAIL", "demo@spine.dev")
DEMO_PASSWORD = os.getenv("SPINE_DEMO_PASSWORD", "spine12345")
DEMO_NAME = "Demo Admin"


def _load_finance_pack():
    # examples/ is not a package; load the pack by path so the demo and the
    # standalone example script share one definition of the desk's rules.
    path = Path(__file__).resolve().parents[1] / "examples" / "finance" / "seed_finance_policies.py"
    spec = importlib.util.spec_from_file_location("seed_finance_policies", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PACK = _load_finance_pack()

# Open around the clock, so the demo behaves the same outside market hours.
# The standalone example script seeds the real 13:30-20:00 UTC window.
DEMO_POLICIES = _PACK.policies(always_open=True)
DEMO_MANDATE = _PACK.MANDATE


async def _wait_for_api(client: httpx.AsyncClient, *, attempts: int = 60) -> None:
    for i in range(attempts):
        try:
            res = await client.get(f"{BASE_URL}/health", timeout=2.0)
            if res.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if i == 0:
            print("Waiting for the API to come up...")
        await asyncio.sleep(1.0)
    raise RuntimeError(f"API at {BASE_URL} never became healthy")


async def _post(client: httpx.AsyncClient, path: str, *, headers: dict, json: dict) -> dict:
    res = await client.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=15.0)
    if res.status_code >= 400:
        raise RuntimeError(f"POST {path} -> {res.status_code}: {res.text}")
    return res.json()


async def main() -> None:
    admin = {"X-API-Key": ADMIN_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        await _wait_for_api(client)

        org = await _post(client, "/v1/orgs", headers=admin, json={"name": "demo-desk", "plan": "starter"})
        org_id = str(org["id"])

        key = await _post(client, f"/v1/orgs/{org_id}/api-keys", headers=admin, json={"name": "quickstart"})
        org_key = str(key["raw_key"])
        org_headers = {"X-Org-Key": org_key, "Content-Type": "application/json"}

        agent = await _post(
            client,
            "/v1/agents/register",
            headers=org_headers,
            json={"name": "rebalancer", "framework": "generic"},
        )
        agent_id = str(agent["id"])

        for policy in DEMO_POLICIES:
            await _post(client, "/v1/policies", headers=org_headers, json={**policy, "agent_id": None})

        session = await _post(
            client,
            "/v1/sessions",
            headers=org_headers,
            json={"agent_id": agent_id, **DEMO_MANDATE},
        )
        session_id = str(session["id"])

    await create_user(
        DB_URL,
        email=DEMO_EMAIL,
        name=DEMO_NAME,
        password=DEMO_PASSWORD,
        org_id=uuid.UUID(org_id),
        role="admin",
    )

    print(
        f"""
{"=" * 70}
  Spine is ready: a demo trading desk.
{"=" * 70}

  Dashboard   http://localhost:4173
  Email       {DEMO_EMAIL}
  Password    {DEMO_PASSWORD}

  API         http://localhost:8000
  API docs    http://localhost:8000/docs

  Seeded {len(DEMO_POLICIES)} policies: restricted list {_PACK.RESTRICTED}, orders
  and amends above 1M need four-eyes sign-off, above 10M are refused,
  unpriced orders go to review, cancels always go through, and two
  market-data feeds are entitled. Everything else is denied by default.
  The trading window is open 24h for the demo.

  The agent's mandate is session {session_id}:
    "{DEMO_MANDATE["goal"]}"

  Send orders through the C++ pre-trade gate:

    export SPINE_ORG_KEY={org_key}
    export SPINE_AGENT_ID={agent_id}
    export SPINE_SESSION_ID={session_id}
    cmake -S sdks/cpp -B sdks/cpp/build && cmake --build sdks/cpp/build
    ./sdks/cpp/build/pretrade_gate

  Or ask directly. This order is allowed; swap AAPL for RSTR and it is
  refused by the restricted list, and both answers are recorded:

    curl -sS -X POST http://localhost:8000/v1/intercept \\
      -H "Content-Type: application/json" \\
      -H "X-Org-Key: {org_key}" \\
      -d '{{"agent_id":"{agent_id}",
           "action":{{"action_type":"order.place","target_resource":"AAPL"}}}}'
{"=" * 70}
"""
    )


if __name__ == "__main__":
    asyncio.run(main())
