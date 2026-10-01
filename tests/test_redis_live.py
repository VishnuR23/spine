"""Hot-path Redis behaviour against a real Redis.

The outage tests prove order checks survive Redis being gone. These prove the
same code still uses Redis when it is there: the policy cache fills and is
read, reviews are queued, and Redis comes back into use after the skip
window. Skipped unless SPINE_TEST_REDIS_URL points at a reachable Redis (CI
provides one).
"""

import asyncio
import os
import time

import pytest
import redis
import sqlalchemy as sa

from spine.config.settings import settings
from spine.core import redis_guard
from spine.core.policy_cache import get_active_policies_for_intercept
from spine.models.policy import Policy
from tests._redis_helpers import point_redis_at
from tests._session_fixture import client, seed_org_and_agent  # noqa: F401
from tests.test_intercept_with_session import _add_allow_read, _make_session

LIVE_URL = os.getenv("SPINE_TEST_REDIS_URL", "")


def _reachable() -> bool:
    if not LIVE_URL:
        return False
    try:
        redis.from_url(LIVE_URL, socket_connect_timeout=0.5).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no live Redis (set SPINE_TEST_REDIS_URL)")


@pytest.fixture()
def live_redis(monkeypatch):
    r = redis.from_url(LIVE_URL)
    r.flushdb()
    point_redis_at(monkeypatch, LIVE_URL)
    yield r
    r.flushdb()
    r.close()
    redis_guard.reset()


def _read(client, org_id, agent_id):
    async def go():
        async with client._SessionLocal() as db:
            return await get_active_policies_for_intercept(db, org_id=org_id, agent_id=agent_id)

    return asyncio.run(go())


def _rename_policies_in_db(client, org_id, name):
    async def go():
        async with client._SessionLocal() as db:
            await db.execute(sa.update(Policy).where(Policy.org_id == org_id).values(name=name))
            await db.commit()

    asyncio.run(go())


def test_policy_cache_fills_and_is_read(client, live_redis):  # noqa: F811
    org_id, agent_id, _ = seed_org_and_agent(client)
    _add_allow_read(client, org_id)

    assert [p["name"] for p in _read(client, org_id, agent_id)] == ["allow-read"]
    assert live_redis.exists(f"spine:policies:cache:{org_id}"), "a miss refills the cache"

    # Change the row behind the cache's back: a cache hit still returns the
    # cached name, which proves the second read came from Redis.
    _rename_policies_in_db(client, org_id, "renamed-in-db")
    assert [p["name"] for p in _read(client, org_id, agent_id)] == ["allow-read"]


def test_session_order_queues_its_review(client, live_redis):  # noqa: F811
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    sid = _make_session(client, key, agent_id)

    r = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={"agent_id": str(agent_id), "session_id": sid, "action": {"action_type": "read", "target_resource": "/x"}},
    )
    assert r.status_code == 200 and r.json()["allowed"] is True
    assert live_redis.llen("celery") == 1, "the review task is on the queue"


def test_redis_is_used_again_after_the_skip_window(client, live_redis, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "redis_retry_after_seconds", 0.05)
    org_id, agent_id, _ = seed_org_and_agent(client)
    _add_allow_read(client, org_id)

    redis_guard.mark_down()
    _read(client, org_id, agent_id)
    assert not live_redis.exists(f"spine:policies:cache:{org_id}"), "skipped while marked down"

    time.sleep(0.1)
    _read(client, org_id, agent_id)
    assert live_redis.exists(f"spine:policies:cache:{org_id}"), "used again once the window passes"
