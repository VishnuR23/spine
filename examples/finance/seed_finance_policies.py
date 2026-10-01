"""Seed a finance policy pack and a trading mandate.

Covers the whole order lifecycle the C++ gate can ask about: new orders and
amends share the same bands, restricted list, and trading hours; cancels are
always allowed, because refusing one keeps risk on.

Creates an org, an API key, an agent, the policies below, and a session whose
declared plan is the agent's mandate. Prints the exports the C++ pre-trade
gate needs.

    make demo                                          # Spine must be running
    python examples/finance/seed_finance_policies.py
    export SPINE_ORG_KEY=...  SPINE_AGENT_ID=...  SPINE_SESSION_ID=...
    ./sdks/cpp/build/pretrade_gate

Two things about the policy engine shape everything here.

Precedence is deny > allow > flag > default-deny. So the restricted-list deny
does not need to know about the allow rules — it wins wherever it matches.
And because `flag` is weaker than `allow`, the notional bands must be distinct
action types rather than one broad "order.place" rule, or a blanket allow
would swallow the review.

The engine matches strings and cannot compare numbers. The client computes
notional and picks a band; the policy governing each band lives here, so
compliance changes a threshold without anyone rebuilding the trading system.

Pass --always-open to widen the trading window to 24 hours, which makes the
demo deterministic outside market hours.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BASE_URL = os.getenv("SPINE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_KEY = os.getenv("ADMIN_API_KEY", "change-me")

# Symbols under restriction — the firm holds material non-public information,
# or an active mandate conflicts. This list belongs in Spine and nowhere else:
# it changes intraday, and a copy compiled into a trading binary is a copy
# that is out of date.
RESTRICTED = ["RSTR", "ACME", "CONFL"]

# Regular US equity trading hours in UTC. 09:30-16:00 America/New_York during
# daylight time. Note that this is a fixed UTC window, so it drifts by an hour
# when New York changes clocks and Spine does not — a real deployment should
# re-seed on the DST boundary, or express the window in exchange local time.
RTH_START_UTC = "13:30"
RTH_END_UTC = "20:00"


def policies(always_open: bool) -> list[dict]:
    window = None if always_open else {"start": RTH_START_UTC, "end": RTH_END_UTC}
    restricted_regex = "^(" + "|".join(RESTRICTED) + ")$"

    pack: list[dict] = [
        # Deny wins over everything, so this one rule covers every order band
        # without having to be repeated in each.
        {
            "name": "Restricted list: no orders in restricted names",
            "rule_type": "action",
            "rule_config": {
                "effect": "deny",
                "action_types": [
                    "order.place",
                    "order.place.large",
                    "order.place.unpriced",
                    "order.place.block",
                    "order.amend",
                    "order.amend.large",
                    "order.amend.unpriced",
                    "order.amend.block",
                ],
                "target_resource_regex": restricted_regex,
            },
        },
        # Above the firm's hard ceiling. Not a review — a refusal.
        {
            "name": "Notional ceiling: refuse orders above 10M",
            "rule_type": "action",
            "rule_config": {"effect": "deny", "action_types": ["order.place.block", "order.amend.block"]},
        },
        # Four-eyes. Flag withholds the order and opens an approval ticket;
        # approving issues a time-boxed grant so the retry goes through.
        {
            "name": "Four-eyes: orders above 1M need human sign-off",
            "rule_type": "action",
            "rule_config": {"effect": "flag", "action_types": ["order.place.large", "order.amend.large"]},
        },
        {
            "name": "Four-eyes: unpriced orders need human sign-off",
            "rule_type": "action",
            "rule_config": {"effect": "flag", "action_types": ["order.place.unpriced", "order.amend.unpriced"]},
        },
        # The ordinary path.
        {
            "name": "Allow ordinary orders during trading hours",
            "rule_type": "action",
            "rule_config": {
                "effect": "allow",
                "action_types": ["order.place", "order.amend"],
                **({"time_window_utc": window} if window else {}),
            },
        },
        # Cancels reduce risk. Allowed at any hour, in any name: a restricted
        # or out-of-hours cancel is still a cancel you want to go through.
        {
            "name": "Cancels: always allowed",
            "rule_type": "action",
            "rule_config": {"effect": "allow", "action_types": ["order.cancel"]},
        },
        # Market data entitlements. Feeds not named here fall through to
        # default-deny, which is the correct posture for licensed data.
        {
            "name": "Market data: entitled feeds only",
            "rule_type": "action",
            "rule_config": {
                "effect": "allow",
                "action_types": ["marketdata.read"],
                "target_resource_regex": "^(us-equities-l1|us-equities-ref)$",
            },
        },
    ]

    if window:
        # The engine matches inside a window, so "outside hours" is expressed
        # as the complementary overnight window rather than a negation.
        # Without this, out-of-hours orders would still be refused — by
        # default-deny — but the audit trail would say "No matching policy"
        # instead of naming the control that stopped them.
        pack.append(
            {
                "name": "No order placement outside trading hours",
                "rule_type": "action",
                "rule_config": {
                    "effect": "deny",
                    "action_types": [
                        "order.place",
                        "order.place.large",
                        "order.place.unpriced",
                        "order.amend",
                        "order.amend.large",
                        "order.amend.unpriced",
                    ],
                    "time_window_utc": {"start": RTH_END_UTC, "end": RTH_START_UTC},
                },
            }
        )

    return pack


# The mandate. Plan-bound review scores each order against this, which is what
# catches an agent whose individual orders are all permitted but whose
# trajectory has left the strategy it was given.
MANDATE = {
    "goal": "Rebalance the US equity book toward the published target weights",
    "constraints": [
        "only trade names already held in the book",
        "no single order above 1M notional without human approval",
        "no trading outside regular US equity hours",
        "no orders in restricted names",
        "amends follow the same limits as new orders",
    ],
    "expected_resources": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"],
    "success_criteria": "book weights within 50bps of target, no restricted names touched",
}


async def _post(client: httpx.AsyncClient, path: str, *, headers: dict, json: dict) -> dict:
    res = await client.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=15.0)
    if res.status_code >= 400:
        raise RuntimeError(f"POST {path} -> {res.status_code}: {res.text}")
    return res.json()


async def main(always_open: bool) -> None:
    admin = {"X-API-Key": ADMIN_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        org = await _post(client, "/v1/orgs", headers=admin, json={"name": "demo-desk", "plan": "starter"})
        org_id = str(org["id"])

        key = await _post(client, f"/v1/orgs/{org_id}/api-keys", headers=admin, json={"name": "trading-gate"})
        org_key = str(key["raw_key"])
        org_headers = {"X-Org-Key": org_key, "Content-Type": "application/json"}

        agent = await _post(
            client,
            "/v1/agents/register",
            headers=org_headers,
            json={"name": "rebalancer", "framework": "generic"},
        )
        agent_id = str(agent["id"])

        pack = policies(always_open)
        for policy in pack:
            await _post(client, "/v1/policies", headers=org_headers, json={**policy, "agent_id": None})

        session = await _post(
            client,
            "/v1/sessions",
            headers=org_headers,
            json={"agent_id": agent_id, **MANDATE},
        )
        session_id = str(session["id"])

    window_note = "24h (--always-open)" if always_open else f"{RTH_START_UTC}-{RTH_END_UTC} UTC"

    print(
        f"""
{"=" * 70}
  Finance policy pack seeded.
{"=" * 70}

  {len(pack)} policies, restricted list {RESTRICTED}, trading window {window_note}.
  Notional bands: review at 1M, refuse at 10M.
  Mandate declared as session {session_id}.

  Run the C++ pre-trade gate against it:

    export SPINE_ORG_KEY={org_key}
    export SPINE_AGENT_ID={agent_id}
    export SPINE_SESSION_ID={session_id}
    ./sdks/cpp/build/pretrade_gate

  Then verify nothing has been altered:

    curl -sS {BASE_URL}/v1/audit/verify -H "X-Org-Key: {org_key}"
{"=" * 70}
"""
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--always-open",
        action="store_true",
        help="widen the trading window to 24h so the demo works outside market hours",
    )
    args = parser.parse_args()
    asyncio.run(main(args.always_open))
