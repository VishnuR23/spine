"""Intercept with session_id: response shape, drift gate, audit chain backwards compat."""

import asyncio
import uuid

import sqlalchemy as sa

from spine.api.deps import get_db
from spine.config.settings import settings
from spine.core.audit_verify import verify_org_chain
from spine.models.policy import Policy
from spine.models.session import Session as SessionModel
from tests._session_fixture import client, seed_org_and_agent  # noqa: F401


def _add_allow_read(client, org_id):
    async def _seed():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        session.add(
            Policy(
                org_id=org_id,
                name="allow-read",
                rule_type="action",
                rule_config={"effect": "allow", "action_types": ["read"]},
            )
        )
        await session.commit()

    asyncio.run(_seed())


def _make_session(client, key, agent_id, goal="Refactor auth"):
    r = client.post(
        "/v1/sessions",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "goal": goal,
            "constraints": ["only touch /src/auth/**"],
            "expected_resources": ["/src/auth/*.ts"],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_intercept_with_session_returns_session_fields(client):
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    sid = _make_session(client, key, agent_id)

    r = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "session_id": sid,
            "action": {"action_type": "read", "target_resource": "/src/auth/login.ts"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] is True
    assert body["session_id"] == sid
    assert body["drift_score"] == 0.0


def test_intercept_without_session_id_unchanged(client):
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    r = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "action": {"action_type": "read", "target_resource": "/x"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is True
    assert body["session_id"] is None
    assert body["drift_score"] is None


def test_intercept_with_bad_session_id_404(client):
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    bogus = str(uuid.uuid4())
    r = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "session_id": bogus,
            "action": {"action_type": "read", "target_resource": "/x"},
        },
    )
    assert r.status_code == 404


def test_drift_gate_hard_blocks_when_threshold_crossed(client):
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    sid = _make_session(client, key, agent_id)

    # Bypass the worker and directly set drift_score above the block threshold,
    # simulating accumulated drift from prior plan evaluations.
    async def _bump():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        await session.execute(
            sa.update(SessionModel)
            .where(SessionModel.id == uuid.UUID(sid))
            .values(drift_score=settings.plan_drift_block_threshold + 0.05)
        )
        await session.commit()

    asyncio.run(_bump())

    r = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "session_id": sid,
            "action": {"action_type": "read", "target_resource": "/src/auth/login.ts"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is False
    assert body["decision"] == "blocked"
    assert "drift" in body["reason"].lower()


def test_audit_chain_verifies_with_and_without_session_id(client):
    """The hash chain must verify whether a row has session_id set or not."""
    org_id, agent_id, key = seed_org_and_agent(client)
    _add_allow_read(client, org_id)
    sid = _make_session(client, key, agent_id)

    # One intercept without session_id, one with.
    client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "action": {"action_type": "read", "target_resource": "/a"},
        },
    )
    client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "session_id": sid,
            "action": {"action_type": "read", "target_resource": "/src/auth/login.ts"},
        },
    )

    async def _verify():
        db_gen = client.app.dependency_overrides[get_db]()
        session = await db_gen.__anext__()
        return await verify_org_chain(session, org_id=org_id)

    res = asyncio.run(_verify())
    assert res.ok is True
    assert res.checked >= 2
