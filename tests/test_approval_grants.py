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


def test_approved_grant_allows_retry_after_flag(client: TestClient):
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    raw_key = "spine_test_grant_key"

    async def seed():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        session.add(ApiKey(org_id=org_id, key_hash=ApiKey.hash_raw_key(raw_key), name="test"))
        session.add(Agent(id=agent_id, org_id=org_id, name="a1", framework="claude-code", is_active=True))
        session.add(
            Policy(
                org_id=org_id,
                name="flag reads",
                rule_type="action",
                rule_config={
                    "effect": "flag",
                    "action_types": ["read"],
                    "target_resource_regex": "^/secret/.*",
                },
                is_active=True,
            )
        )
        await session.commit()

    import asyncio

    asyncio.run(seed())

    headers = {"X-Org-Key": raw_key}
    body = {
        "agent_id": str(agent_id),
        "action": {"action_type": "read", "target_resource": "/secret/data", "metadata": {}},
    }
    r1 = client.post("/v1/intercept", json=body, headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["allowed"] is False
    assert d1["decision"] == "flagged"
    approval_id = d1["approval_id"]
    assert approval_id

    decide = client.post(
        f"/v1/approvals/{approval_id}/decide",
        params={"org_id": str(org_id)},
        json={"action": "approve", "decided_by": "admin@test.com"},
        headers=headers,
    )
    assert decide.status_code == 200

    r2 = client.post("/v1/intercept", json=body, headers=headers)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["allowed"] is True
    assert d2["decision"] == "allowed"
    assert "Human approval" in d2["reason"]
