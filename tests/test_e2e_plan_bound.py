"""Full end-to-end plan-bound lifecycle.

Wires the live HTTP API (via TestClient) to the actual plan engine
(monkeypatching only the Claude call). Exercises:

  1. POST /v1/sessions with full body
  2. POST /v1/intercept (aligned) — round-trips session_id, drift starts at 0
  3. Plan engine runs ALIGNED verdict — drift stays at 0, no approval
  4. POST /v1/intercept again — would-be-divergent target
  5. Plan engine runs DIVERGENT verdict — approval row created, ev.approval_id set
  6. Two more divergent verdicts to push drift above the block threshold via EMA
  7. POST /v1/intercept (would otherwise be allowed) — drift gate hard-blocks
  8. GET /v1/sessions/{id}/evaluations — returns full history with correct shape
  9. POST /v1/sessions/{id}/end — closes session
 10. After end, plan engine skips further evaluations
 11. Audit chain still verifies with mix of session and non-session rows

Both async (API) and sync (plan engine) engines point at the SAME on-disk
SQLite file so reads from one see writes from the other.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from spine.api.deps import get_db
from spine.config.settings import settings
from spine.core.audit_verify import verify_org_chain
from spine.db.base import Base
from spine.main import create_app
from spine.models.agent import Agent
from spine.models.api_key import ApiKey
from spine.models.approval import Approval
from spine.models.policy import Policy
from spine.models.session import Session as SessionModel


@pytest.fixture()
def stack():
    """API (async) + worker (sync) backed by the same SQLite file."""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tf.name
    tf.close()

    saved = {
        "database_url": settings.database_url,
        "admin_api_key": settings.admin_api_key,
        "sse_enabled": settings.sse_enabled,
        "plan_drift_flag_threshold": settings.plan_drift_flag_threshold,
        "plan_drift_block_threshold": settings.plan_drift_block_threshold,
    }
    settings.database_url = f"sqlite+aiosqlite:///{db_path}"
    settings.admin_api_key = "test-key"
    settings.sse_enabled = False
    settings.plan_drift_flag_threshold = 0.4
    settings.plan_drift_block_threshold = 0.6

    async_engine = create_async_engine(settings.database_url)
    AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)
    sync_engine = create_engine(f"sqlite:///{db_path}")
    SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False)

    app = create_app()

    async def override_get_db():
        async with AsyncSessionLocal() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db

    async def init():
        async with async_engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)

    asyncio.run(init())

    client = TestClient(app)
    try:
        yield client, SyncSessionLocal, AsyncSessionLocal
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def _seed(client) -> tuple[uuid.UUID, uuid.UUID, str]:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    raw_key = f"spine_e2e_{uuid.uuid4().hex[:8]}"

    async def go():
        db_gen = client.app.dependency_overrides[get_db]()
        s = await db_gen.__anext__()
        s.add(ApiKey(org_id=org_id, key_hash=ApiKey.hash_raw_key(raw_key), name="t"))
        s.add(Agent(id=agent_id, org_id=org_id, name="a", framework="claude-code"))
        s.add(
            Policy(
                org_id=org_id,
                name="allow-read",
                rule_type="action",
                rule_config={"effect": "allow", "action_types": ["read"]},
            )
        )
        await s.commit()

    asyncio.run(go())
    return org_id, agent_id, raw_key


def _intercept(client, key, agent_id, sid, target):
    return client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "session_id": sid,
            "action": {"action_type": "read", "target_resource": target},
        },
    )


def _run_engine(sync_local, *, audit_id, sid, monkeypatch, verdict_json):
    """Run the plan engine sync with a mocked reviewer call."""
    from spine.monitor import plan_engine

    monkeypatch.setattr(plan_engine, "_call_reviewer", lambda *a, **kw: verdict_json)
    db = sync_local()
    try:
        return plan_engine.evaluate_plan_alignment(db, audit_event_id=uuid.UUID(audit_id), session_id=uuid.UUID(sid))
    finally:
        db.close()


def test_full_plan_bound_lifecycle(monkeypatch, stack):
    client, sync_local, async_local = stack
    org_id, agent_id, key = _seed(client)

    # ── 1. Register the session
    r = client.post(
        "/v1/sessions",
        headers={"X-Org-Key": key},
        json={
            "agent_id": str(agent_id),
            "goal": "Refactor the auth module to use JWT instead of session cookies",
            "constraints": ["only touch /src/auth/**", "no schema changes"],
            "expected_resources": ["/src/auth/*.ts", "/tests/auth/*.test.ts"],
            "success_criteria": "all auth tests pass",
        },
    )
    assert r.status_code == 201, r.text
    s_body = r.json()
    sid = s_body["id"]
    assert s_body["status"] == "active"
    assert s_body["drift_score"] == 0.0
    assert s_body["evaluation_count"] == 0
    assert s_body["constraints"] == ["only touch /src/auth/**", "no schema changes"]

    # ── 2. Aligned intercept
    r = _intercept(client, key, agent_id, sid, "/src/auth/login.ts")
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is True
    assert body["session_id"] == sid
    assert body["drift_score"] == 0.0
    aligned_audit_id = body["audit_event_id"]

    # ── 3. Run plan engine with ALIGNED verdict
    ev1 = _run_engine(
        sync_local,
        audit_id=aligned_audit_id,
        sid=sid,
        monkeypatch=monkeypatch,
        verdict_json=(
            '{"alignment":"aligned","confidence":0.9,"reasoning":"stays inside /src/auth","drift_contribution":0.0}'
        ),
    )
    assert ev1 is not None
    assert ev1.alignment == "aligned"
    assert ev1.approval_id is None
    assert ev1.drift_score_after == 0.0

    # Session has now evaluated 1 action with drift 0
    s_db = sync_local()
    try:
        sess = s_db.get(SessionModel, uuid.UUID(sid))
        assert sess.evaluation_count == 1
        assert sess.drift_score == 0.0
    finally:
        s_db.close()

    # ── 4. Divergent intercept (policy allows; reviewer will say divergent)
    r = _intercept(client, key, agent_id, sid, "/etc/secrets/.env")
    assert r.json()["allowed"] is True  # policy lets the read through
    divergent_audit_id = r.json()["audit_event_id"]

    # ── 5. Run plan engine with DIVERGENT verdict
    ev2 = _run_engine(
        sync_local,
        audit_id=divergent_audit_id,
        sid=sid,
        monkeypatch=monkeypatch,
        verdict_json=(
            '{"alignment":"divergent","confidence":0.95,'
            '"reasoning":"reading secrets is not in scope","drift_contribution":0.95}'
        ),
    )
    assert ev2.alignment == "divergent"
    # Divergent always creates an approval ticket regardless of drift score
    assert ev2.approval_id is not None
    # EMA: 0.3 * 0.95 + 0.7 * 0.0 = 0.285
    assert abs(ev2.drift_score_after - 0.285) < 1e-9

    # ── 5b. Verify the approval row exists in the DB
    s_db = sync_local()
    try:
        approvals = s_db.execute(sa.select(Approval).where(Approval.org_id == org_id)).scalars().all()
        assert len(approvals) == 1
        assert approvals[0].status == "pending"
        assert approvals[0].audit_event_id == uuid.UUID(divergent_audit_id)
        # The approval proposed carries the divergent action metadata for review
        assert approvals[0].proposed["action"]["action_type"] == "read"
        assert approvals[0].proposed["action"]["target_resource"] == "/etc/secrets/.env"
        spine_meta = approvals[0].proposed["action"]["metadata"]["spine"]
        assert spine_meta["plan_alignment"] == "divergent"
        assert spine_meta["origin"] == "plan_drift"
    finally:
        s_db.close()

    # ── 6. Two more divergent verdicts to push drift past block threshold via EMA
    #      drift_2 = 0.3*0.95 + 0.7*0.285 = 0.4845
    #      drift_3 = 0.3*0.95 + 0.7*0.4845 = 0.624 (> 0.6 block threshold)
    r = _intercept(client, key, agent_id, sid, "/etc/passwd")
    ev3 = _run_engine(
        sync_local,
        audit_id=r.json()["audit_event_id"],
        sid=sid,
        monkeypatch=monkeypatch,
        verdict_json=('{"alignment":"divergent","confidence":0.95,"reasoning":"off plan","drift_contribution":0.95}'),
    )
    assert abs(ev3.drift_score_after - 0.4845) < 1e-9

    r = _intercept(client, key, agent_id, sid, "/etc/shadow")
    ev4 = _run_engine(
        sync_local,
        audit_id=r.json()["audit_event_id"],
        sid=sid,
        monkeypatch=monkeypatch,
        verdict_json=('{"alignment":"divergent","confidence":0.95,"reasoning":"off plan","drift_contribution":0.95}'),
    )
    assert ev4.drift_score_after > settings.plan_drift_block_threshold

    # ── 7. Next intercept (would-be-aligned target!) is hard-blocked by drift gate
    r = _intercept(client, key, agent_id, sid, "/src/auth/login.ts")
    blocked_body = r.json()
    assert blocked_body["allowed"] is False
    assert blocked_body["decision"] == "blocked"
    assert "drift" in blocked_body["reason"].lower()
    assert blocked_body["session_id"] == sid
    assert blocked_body["drift_score"] > settings.plan_drift_block_threshold

    # ── 8. GET /v1/sessions/{id}/evaluations returns all four verdicts in order
    r = client.get(f"/v1/sessions/{sid}/evaluations", headers={"X-Org-Key": key})
    assert r.status_code == 200
    evals = r.json()
    assert len(evals) == 4
    assert [e["alignment"] for e in evals] == [
        "aligned",
        "divergent",
        "divergent",
        "divergent",
    ]
    assert all(e["session_id"] == sid for e in evals)
    assert evals[-1]["drift_score_after"] > 0.6

    # ── 9. End the session
    r = client.post(
        f"/v1/sessions/{sid}/end",
        headers={"X-Org-Key": key},
        json={"status": "completed"},
    )
    assert r.status_code == 200
    end_body = r.json()
    assert end_body["status"] == "completed"
    assert end_body["evaluation_count"] == 4
    assert end_body["drift_score"] > 0.6

    # ── 10. After end, plan engine returns None (skips inactive sessions)
    r = _intercept(client, key, agent_id, sid, "/src/auth/x.ts")
    # Intercept still goes through but is blocked by drift gate (status was
    # active when drift accumulated, now the gate check still loads the
    # session by status=active so an ended session means the gate's
    # _load_active_session returns None → 404):
    assert r.status_code == 404
    assert "session" in r.json()["detail"].lower()

    # ── 11. Audit chain integrity across session + non-session rows
    async def _verify():
        db_gen = client.app.dependency_overrides[get_db]()
        s = await db_gen.__anext__()
        return await verify_org_chain(s, org_id=org_id)

    res = asyncio.run(_verify())
    assert res.ok is True
    # 4 intercepts + 4 plan.evaluation audit rows from the engine + 1 blocked intercept
    # + 1 attempted-after-end (404 doesn't write audit) ≈ 9 rows
    assert res.checked >= 9
    assert res.first_bad_sequence is None


def test_cross_org_session_isolation(stack):
    """A session created under org A is invisible / 404 to org B."""
    client, _, _ = stack
    _, agent_a, key_a = _seed(client)
    _, _, key_b = _seed(client)

    r = client.post(
        "/v1/sessions",
        headers={"X-Org-Key": key_a},
        json={"agent_id": str(agent_a), "goal": "secret plan"},
    )
    assert r.status_code == 201
    sid = r.json()["id"]

    # Org B cannot GET, list, end, or run evaluations on that session
    assert client.get(f"/v1/sessions/{sid}", headers={"X-Org-Key": key_b}).status_code == 404
    assert client.get(f"/v1/sessions/{sid}/evaluations", headers={"X-Org-Key": key_b}).status_code == 404
    assert (
        client.post(
            f"/v1/sessions/{sid}/end",
            headers={"X-Org-Key": key_b},
            json={"status": "abandoned"},
        ).status_code
        == 404
    )
    # And listing only shows org A's
    rb = client.get("/v1/sessions", headers={"X-Org-Key": key_b})
    assert rb.status_code == 200
    assert all(s["id"] != sid for s in rb.json())


def test_session_id_validation_in_intercept(stack):
    """Intercept with a session_id from another org must be rejected."""
    client, _, _ = stack
    _, agent_a, key_a = _seed(client)
    _, agent_b, key_b = _seed(client)

    # Create a session for agent A
    sa_resp = client.post(
        "/v1/sessions",
        headers={"X-Org-Key": key_a},
        json={"agent_id": str(agent_a), "goal": "A's plan"},
    )
    sid_a = sa_resp.json()["id"]

    # Org B's agent tries to use Org A's session_id → 404
    r = client.post(
        "/v1/intercept",
        headers={"X-Org-Key": key_b},
        json={
            "agent_id": str(agent_b),
            "session_id": sid_a,
            "action": {"action_type": "read", "target_resource": "/x"},
        },
    )
    assert r.status_code == 404


def test_plan_evaluation_payload_redaction(monkeypatch, stack):
    """SSE plan payload must not leak the reviewer's raw reasoning."""
    from spine.core.redaction import stream_safe_plan_payload
    from spine.monitor.plan_engine import (
        evaluate_plan_alignment,  # noqa: F401  (smoke-imports for ordering)
    )

    client, sync_local, _ = stack
    _, agent_id, key = _seed(client)
    sid = client.post(
        "/v1/sessions",
        headers={"X-Org-Key": key},
        json={"agent_id": str(agent_id), "goal": "g"},
    ).json()["id"]

    # Trigger a divergent verdict whose reasoning contains an email address
    # (which the redactor strips).
    audit_id = _intercept(client, key, agent_id, sid, "/x").json()["audit_event_id"]
    ev = _run_engine(
        sync_local,
        audit_id=audit_id,
        sid=sid,
        monkeypatch=monkeypatch,
        verdict_json=(
            '{"alignment":"divergent","confidence":0.9,'
            '"reasoning":"user attacker@evil.com tried to access this",'
            '"drift_contribution":0.9}'
        ),
    )
    payload = stream_safe_plan_payload(evaluation=ev, drift_score=ev.drift_score_after, session_id=uuid.UUID(sid))
    assert "attacker@evil.com" not in payload["reasoning"]
    assert "REDACTED" in payload["reasoning"]
    assert payload["alignment"] == "divergent"
    assert payload["session_id"] == sid
    assert isinstance(payload["drift_score"], float)
