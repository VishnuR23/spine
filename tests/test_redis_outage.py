"""Order checks stay fast when Redis is down.

Redis backs the policy cache, the live event stream, and the reviewer's task
queue. None of those may hold an order check hostage: when Redis refuses
connections or stops answering, intercept must fall through to Postgres and
skip the queue within a small, fixed time, or the C++ gate's latency budget
expires and every order fails closed.
"""

import asyncio
import time

import pytest

from spine.core import redis_guard
from spine.core.policy_cache import get_active_policies_for_intercept
from tests._redis_helpers import point_redis_at
from tests._session_fixture import client, seed_org_and_agent  # noqa: F401
from tests.test_intercept_with_session import _add_allow_read, _make_session

REFUSED = "redis://127.0.0.1:1/0"  # nothing listens: connection refused at once
UNREACHABLE = "redis://10.255.255.1:6379/0"  # non-routable: packets just vanish


@pytest.fixture()
def redis_at(monkeypatch):
    yield lambda url: point_redis_at(monkeypatch, url)
    redis_guard.reset()


def test_order_in_a_session_is_fast_when_redis_refuses(client, redis_at):  # noqa: F811
    redis_at(REFUSED)
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    sid = _make_session(client, key, agent_id)

    for _ in range(3):
        started = time.perf_counter()
        r = client.post(
            "/v1/intercept",
            headers={"X-Org-Key": key},
            json={
                "agent_id": str(agent_id),
                "session_id": sid,
                "action": {"action_type": "read", "target_resource": "/x"},
            },
        )
        took = time.perf_counter() - started
        assert r.status_code == 200 and r.json()["allowed"] is True
        assert took < 0.25, f"intercept took {took * 1000:.0f} ms with Redis refusing"


def _policy_read(client, org_id, agent_id, *, limit_seconds):
    async def go():
        async with client._SessionLocal() as db:
            return await asyncio.wait_for(
                get_active_policies_for_intercept(db, org_id=org_id, agent_id=agent_id),
                limit_seconds,
            )

    started = time.perf_counter()
    try:
        policies = asyncio.run(go())
    except asyncio.TimeoutError:
        pytest.fail(f"policy read still waiting on Redis after {limit_seconds} s")
    return policies, time.perf_counter() - started


def test_policy_read_is_bounded_when_redis_is_unreachable(client, redis_at):  # noqa: F811
    redis_at(UNREACHABLE)
    org_id, agent_id, _ = seed_org_and_agent(client)
    _add_allow_read(client, org_id)

    policies, took = _policy_read(client, org_id, agent_id, limit_seconds=2)
    assert [p["name"] for p in policies] == ["allow-read"], "falls through to Postgres"
    assert took < 0.5, f"first read took {took * 1000:.0f} ms"


def test_unreachable_redis_is_skipped_after_the_first_failure(client, redis_at):  # noqa: F811
    redis_at(UNREACHABLE)
    org_id, agent_id, _ = seed_org_and_agent(client)
    _add_allow_read(client, org_id)

    _policy_read(client, org_id, agent_id, limit_seconds=2)
    _, took = _policy_read(client, org_id, agent_id, limit_seconds=2)
    assert took < 0.02, f"second read took {took * 1000:.0f} ms; Redis should be skipped"
