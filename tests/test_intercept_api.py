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
from spine.models.policy import Policy


@pytest.fixture()
def client():
    # override settings for tests
    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.admin_api_key = "test-key"

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

    import asyncio

    asyncio.run(init_models())

    return TestClient(app)


def test_intercept_allowed(client: TestClient):
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    # seed via direct DB session using the app override
    async def seed():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        raw_key = "spine_test_key"
        session.add(ApiKey(org_id=org_id, key_hash=ApiKey.hash_raw_key(raw_key), name="test"))
        session.add(Agent(id=agent_id, org_id=org_id, name="a1", framework="generic"))
        session.add(
            Policy(
                org_id=org_id,
                name="allow-read",
                rule_type="action",
                rule_config={"effect": "allow", "action_types": ["read"]},
            )
        )
        await session.commit()

    import asyncio

    asyncio.run(seed())

    resp = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": "spine_test_key"},
        json={
            "agent_id": str(agent_id),
            "action": {"action_type": "read", "target_resource": "/x"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True
    assert body["decision"] == "allowed"
    assert "audit_event_id" in body
