import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.deps import get_db
from spine.auth.passwords import hash_password
from spine.config.settings import settings
from spine.db.base import Base
from spine.main import create_app
from spine.models.organization import Organization
from spine.models.user import Membership, User


@pytest.fixture()
def client():
    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.admin_api_key = "test-key"
    settings.jwt_secret = "test-jwt-secret-at-least-32-bytes!"

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    import asyncio

    asyncio.run(_init(engine))

    return TestClient(app)


async def _init(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def seeded_client(client: TestClient):
    """Client with a pre-created user + org."""
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def seed():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        session.add(Organization(id=org_id, name="Test Org"))
        session.add(
            User(
                id=user_id,
                email="alice@test.com",
                password_hash=hash_password("correcthorse"),
                name="Alice",
            )
        )
        session.add(Membership(user_id=user_id, org_id=org_id, role="admin"))
        await session.commit()

    import asyncio

    asyncio.run(seed())

    return client, {"org_id": org_id, "user_id": user_id}


def test_login_success(seeded_client):
    client, _info = seeded_client
    resp = client.post(
        "/v1/auth/login",
        json={"email": "alice@test.com", "password": "correcthorse"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["user"]["email"] == "alice@test.com"
    assert body["user"]["role"] == "admin"
    assert body["user"]["org_name"] == "Test Org"


def test_login_wrong_password(seeded_client):
    client, _info = seeded_client
    resp = client.post(
        "/v1/auth/login",
        json={"email": "alice@test.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401


def test_login_unknown_email(seeded_client):
    client, _info = seeded_client
    resp = client.post(
        "/v1/auth/login",
        json={"email": "nobody@test.com", "password": "anything"},
    )
    assert resp.status_code == 401


def test_me_with_valid_token(seeded_client):
    client, _info = seeded_client
    login_resp = client.post(
        "/v1/auth/login",
        json={"email": "alice@test.com", "password": "correcthorse"},
    )
    token = login_resp.json()["token"]

    me_resp = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "alice@test.com"


def test_me_without_token(client):
    resp = client.get("/v1/auth/me")
    assert resp.status_code == 401


def test_me_with_invalid_token(client):
    resp = client.get("/v1/auth/me", headers={"Authorization": "Bearer garbage-token"})
    assert resp.status_code == 401


def test_jwt_grants_org_access(seeded_client):
    """Verify that a JWT token works for org-scoped endpoints (via OrgIdDep)."""
    client, _info = seeded_client
    login_resp = client.post(
        "/v1/auth/login",
        json={"email": "alice@test.com", "password": "correcthorse"},
    )
    token = login_resp.json()["token"]

    resp = client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
