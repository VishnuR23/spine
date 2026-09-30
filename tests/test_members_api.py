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
    settings.admin_api_key = "test-admin-key"
    settings.jwt_secret = "test-jwt-secret-distinct"
    settings.environment = "local"

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
def org_with_users(client: TestClient):
    org_id = uuid.uuid4()

    async def seed():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        session.add(Organization(id=org_id, name="Acme"))
        admin = User(
            email="admin@acme.com",
            name="Admin",
            password_hash=hash_password("password123"),
        )
        session.add(admin)
        await session.flush()
        session.add(Membership(user_id=admin.id, org_id=org_id, role="admin"))
        member = User(
            email="member@acme.com",
            name="Member",
            password_hash=hash_password("password123"),
        )
        session.add(member)
        await session.flush()
        session.add(Membership(user_id=member.id, org_id=org_id, role="member"))
        await session.commit()

    import asyncio

    asyncio.run(seed())
    return client


def _token(client: TestClient, email: str, password: str = "password123") -> str:
    res = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["token"]


def test_member_cannot_list_members(org_with_users: TestClient):
    client = org_with_users
    token = _token(client, "member@acme.com")
    res = client.get("/v1/members", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


def test_admin_lists_and_creates_member(org_with_users: TestClient):
    client = org_with_users
    token = _token(client, "admin@acme.com")

    created = client.post(
        "/v1/members",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": "new@acme.com",
            "name": "New User",
            "password": "temppass99",
            "role": "member",
        },
    )
    assert created.status_code == 201
    assert created.json()["email"] == "new@acme.com"

    listed = client.get("/v1/members", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200
    emails = {m["email"] for m in listed.json()}
    assert "new@acme.com" in emails
    assert "admin@acme.com" in emails
