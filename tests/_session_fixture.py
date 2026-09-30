"""Shared in-memory test app fixture for session/plan-bound tests."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.deps import get_db
from spine.config.settings import settings
from spine.db.base import Base
from spine.main import create_app
from spine.models.agent import Agent
from spine.models.api_key import ApiKey
from spine.models.organization import Organization


@pytest.fixture()
def client():
    # Save settings we mutate so we don't pollute other tests.
    saved_db = settings.database_url
    saved_admin = settings.admin_api_key
    saved_sse = settings.sse_enabled

    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.admin_api_key = "test-key"
    settings.sse_enabled = False  # don't try to publish to Redis

    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async def init_models():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init_models())
    c = TestClient(app)
    c._engine = engine  # type: ignore[attr-defined]
    c._SessionLocal = SessionLocal  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        settings.database_url = saved_db
        settings.admin_api_key = saved_admin
        settings.sse_enabled = saved_sse


def seed_org_and_agent(client: TestClient) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Seed an org + org_key + agent. Returns (org_id, agent_id, raw_key)."""
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    raw_key = f"spine_test_{uuid.uuid4().hex[:8]}"

    async def _seed():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        session.add(Organization(id=org_id, name=f"test-{org_id.hex[:6]}", plan="starter"))
        session.add(ApiKey(org_id=org_id, key_hash=ApiKey.hash_raw_key(raw_key), name="test"))
        session.add(Agent(id=agent_id, org_id=org_id, name="a1", framework="generic"))
        await session.commit()

    asyncio.run(_seed())
    return org_id, agent_id, raw_key
