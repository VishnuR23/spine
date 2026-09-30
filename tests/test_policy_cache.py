"""Policy cache: Redis-less fallback, filtering by agent_id, invalidation."""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.config.settings import settings
from spine.core.policy_cache import (
    _filter_for_agent,
    get_active_policies_for_intercept,
    invalidate,
)
from spine.db.base import Base
from spine.models.policy import Policy


@pytest.fixture()
def async_db():
    """Async DB on a private in-memory SQLite. Redis is disabled via empty URL."""
    saved_db = settings.database_url
    saved_redis = settings.redis_url

    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.redis_url = ""  # disable cache → falls through to DB

    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init())
    try:
        yield SessionLocal
    finally:
        settings.database_url = saved_db
        settings.redis_url = saved_redis


def test_filter_for_agent_includes_org_wide_and_own_agent_only():
    aid = uuid.uuid4()
    other = uuid.uuid4()
    policies = [
        {"id": "1", "agent_id": None, "name": "org-wide", "rule_type": None, "rule_config": {}, "is_active": True},
        {"id": "2", "agent_id": str(aid), "name": "mine", "rule_type": None, "rule_config": {}, "is_active": True},
        {"id": "3", "agent_id": str(other), "name": "other", "rule_type": None, "rule_config": {}, "is_active": True},
        {"id": "4", "agent_id": None, "name": "inactive", "rule_type": None, "rule_config": {}, "is_active": False},
    ]
    result = _filter_for_agent(policies, agent_id=aid)
    names = sorted(p["name"] for p in result)
    assert names == ["mine", "org-wide"]


def test_redis_less_falls_through_to_db(async_db):
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async def go():
        async with async_db() as s:
            s.add(
                Policy(
                    org_id=org_id,
                    name="p1",
                    rule_type="action",
                    rule_config={"effect": "allow", "action_types": ["read"]},
                )
            )
            await s.commit()
            rows = await get_active_policies_for_intercept(s, org_id=org_id, agent_id=agent_id)
            return rows

    rows = asyncio.run(go())
    assert len(rows) == 1
    assert rows[0]["name"] == "p1"
    assert rows[0]["rule_config"] == {"effect": "allow", "action_types": ["read"]}


def test_invalidate_is_safe_when_redis_disabled(async_db):
    """Invalidate must not raise when Redis is unconfigured."""

    async def go():
        await invalidate(uuid.uuid4())

    asyncio.run(go())  # no exception = pass
