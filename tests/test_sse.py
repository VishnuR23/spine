import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.deps import get_db
from spine.auth.passwords import hash_password
from spine.config.settings import settings
from spine.core.event_bus import publish_event_sync
from spine.db.base import Base
from spine.main import create_app
from spine.models.organization import Organization
from spine.models.user import Membership, User
from spine.schemas.events import SpineStreamEvent


@pytest.fixture()
def seeded_client():
    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.admin_api_key = "test-key"
    settings.jwt_secret = "test-jwt-secret"
    settings.sse_enabled = True
    settings.sse_heartbeat_seconds = 1

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(Organization(id=org_id, name="SSE Org"))
            session.add(
                User(
                    id=user_id,
                    email="sse@test.com",
                    password_hash=hash_password("secret"),
                    name="SSE User",
                )
            )
            session.add(Membership(user_id=user_id, org_id=org_id, role="admin"))
            await session.commit()

    import asyncio

    asyncio.run(init_db())
    client = TestClient(app)

    login = client.post("/v1/auth/login", json={"email": "sse@test.com", "password": "secret"})
    assert login.status_code == 200
    token = login.json()["token"]
    return client, token, org_id


def test_publish_event_sync_encodes_spine_stream_event():
    org_id = uuid.uuid4()
    published: list[tuple[str, str]] = []

    fake = MagicMock()

    def publish(channel: str, message: str) -> int:
        published.append((channel, message))
        return 1

    fake.publish = publish
    fake.close = MagicMock()

    with patch("redis.from_url", return_value=fake):
        publish_event_sync(org_id, "audit", {"id": "evt-1", "action_type": "test"})

    assert len(published) == 1
    assert published[0][0] == f"spine:events:{org_id}"
    evt = SpineStreamEvent.model_validate_json(published[0][1])
    assert evt.type == "audit"
    assert evt.data["id"] == "evt-1"


def test_audit_stream_emits_connected_and_events(seeded_client):
    client, token, org_id = seeded_client

    async def fake_subscribe(_org_id: uuid.UUID):
        yield SpineStreamEvent(
            type="audit",
            org_id=org_id,
            data={"id": "live-1", "action_type": "intercept", "agent_id": str(uuid.uuid4())},
        )

    with patch("spine.api.routes.audit.subscribe_org_events", fake_subscribe):
        with client.stream(
            "GET",
            "/v1/audit/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            body = "".join(resp.iter_text())
            assert "event: connected" in body
            assert "event: audit" in body
            assert "live-1" in body


def test_audit_stream_disabled_returns_503(seeded_client):
    client, token, _org_id = seeded_client
    settings.sse_enabled = False
    try:
        resp = client.get("/v1/audit/stream", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 503
    finally:
        settings.sse_enabled = True
