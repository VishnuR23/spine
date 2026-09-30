import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.deps import get_db
from spine.auth.passwords import hash_password
from spine.config.settings import settings
from spine.core.redaction import api_safe_audit_metadata, redact_string, redact_value
from spine.core.url_validation import validate_public_webhook_url
from spine.db.base import Base
from spine.main import create_app
from spine.models.organization import Organization
from spine.models.user import Membership, User


def test_redact_sensitive_keys_and_patterns():
    data = {
        "email": "user@example.com",
        "password": "hunter2",
        "nested": {"api_key": "sk-live-abc"},
        "note": "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
    }
    out = redact_value(data)
    assert out["password"] == "[REDACTED]"
    assert out["nested"]["api_key"] == "[REDACTED]"
    assert "[REDACTED]" in out["email"]
    assert "Bearer [REDACTED]" in redact_string(data["note"])


def test_cap_metadata_size():
    big = {"k": "x" * 9000}
    capped = api_safe_audit_metadata(big)
    assert capped.get("_truncated") or len(str(capped)) < 9000


def test_webhook_url_blocks_private_ip():
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with pytest.raises(ValueError, match="private"):
            validate_public_webhook_url("https://evil.example.com/hook", require_https=True)


@pytest.fixture()
def auth_clients():
    settings.database_url = "sqlite+aiosqlite:///:memory:"
    settings.admin_api_key = "test-admin-key-32chars-minimum!!"
    settings.jwt_secret = "test-jwt-secret-32chars-minimum!!!!"
    settings.environment = "local"
    settings.metrics_require_admin_key = True

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    org_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    member_id = uuid.uuid4()

    async def init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(Organization(id=org_id, name="Sec Org"))
            session.add(
                User(
                    id=admin_id,
                    email="admin@example.com",
                    password_hash=hash_password("adminpass"),
                    name="Admin",
                )
            )
            session.add(
                User(
                    id=member_id,
                    email="member@example.com",
                    password_hash=hash_password("memberpass"),
                    name="Member",
                )
            )
            session.add(Membership(user_id=admin_id, org_id=org_id, role="admin"))
            session.add(Membership(user_id=member_id, org_id=org_id, role="member"))
            await session.commit()

    import asyncio

    asyncio.run(init_db())
    client = TestClient(app)

    def login(email: str, password: str) -> str:
        r = client.post("/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200
        return r.json()["token"]

    return {
        "client": client,
        "org_id": org_id,
        "admin_token": login("admin@example.com", "adminpass"),
        "member_token": login("member@example.com", "memberpass"),
    }


def test_metrics_requires_admin_key(auth_clients):
    client = auth_clients["client"]
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"X-API-Key": settings.admin_api_key}).status_code == 200


def test_approvals_cross_org_denied_for_jwt_admin(auth_clients):
    client = auth_clients["client"]
    other_org = uuid.uuid4()
    resp = client.get(
        f"/v1/approvals?org_id={other_org}",
        headers={"Authorization": f"Bearer {auth_clients['admin_token']}"},
    )
    assert resp.status_code == 403


def test_approvals_accessible_for_org_admin(auth_clients):
    client = auth_clients["client"]
    org_id = auth_clients["org_id"]
    resp = client.get(
        f"/v1/approvals?org_id={org_id}",
        headers={"Authorization": f"Bearer {auth_clients['admin_token']}"},
    )
    assert resp.status_code == 200
