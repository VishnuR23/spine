"""Worker task: retries, then fails closed once retries are exhausted."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spine.config.settings import settings
from spine.db.base import Base
from spine.models.agent import Agent
from spine.models.approval import Approval
from spine.models.audit_event import AuditEvent
from spine.models.session import Session as SessionModel
from spine.worker.tasks import evaluate_plan_alignment_task


@pytest.fixture()
def SessionLocal(monkeypatch):
    saved_sse = settings.sse_enabled
    settings.sse_enabled = False
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("spine.worker.tasks.get_sync_session", factory)
    try:
        yield factory
    finally:
        engine.dispose()
        settings.sse_enabled = saved_sse


def _seed(SessionLocal):
    org_id, agent_id, sess_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with SessionLocal() as db:
        db.add(Agent(id=agent_id, org_id=org_id, name="a", framework="generic"))
        db.add(
            SessionModel(
                id=sess_id,
                org_id=org_id,
                agent_id=agent_id,
                goal="g",
                constraints=[],
                expected_resources=[],
                status="active",
                drift_score=0.0,
                evaluation_count=0,
            )
        )
        audit = AuditEvent(
            agent_id=agent_id,
            org_id=org_id,
            action_type="read",
            target_resource="/x",
            policy_decision="allowed",
            sequence=1,
            prev_hash=None,
            event_hash="x" * 64,
            session_id=sess_id,
        )
        db.add(audit)
        db.commit()
        return audit.id, sess_id


def test_exhausted_retries_flag_the_action_for_a_human(monkeypatch, SessionLocal):
    audit_id, sess_id = _seed(SessionLocal)
    calls = {"n": 0}

    def broken_reviewer(_sys, _user):
        calls["n"] += 1
        return "not json"

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", broken_reviewer)

    result = evaluate_plan_alignment_task.apply(args=[str(audit_id), str(sess_id)])

    assert result.failed()
    assert calls["n"] == evaluate_plan_alignment_task.max_retries + 1
    with SessionLocal() as db:
        approvals = db.query(Approval).all()
        assert len(approvals) == 1
        assert approvals[0].status == "pending"
        assert approvals[0].proposed["action"]["metadata"]["spine"]["origin"] == "plan_review_failed"
        failed = db.query(AuditEvent).filter(AuditEvent.action_type == "plan.evaluation_failed").one()
        assert failed.metadata_["error_type"] == "ValueError"


def test_success_after_a_retry_does_not_flag(monkeypatch, SessionLocal):
    audit_id, sess_id = _seed(SessionLocal)
    calls = {"n": 0}

    def flaky_reviewer(_sys, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return '{"alignment": "aligned", "confidence": 0.9, "reasoning": "ok", "drift_contribution": 0.0}'

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", flaky_reviewer)

    result = evaluate_plan_alignment_task.apply(args=[str(audit_id), str(sess_id)])

    assert result.successful()
    assert result.result["alignment"] == "aligned"
    with SessionLocal() as db:
        assert db.query(Approval).count() == 0


def test_failure_row_keeps_the_audit_chain_verifiable(tmp_path):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from spine.core.audit_verify import verify_org_chain
    from spine.core.sync_audit_logger import log_event_sync
    from spine.monitor.plan_engine import flag_failed_review

    db_file = tmp_path / "chain.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    saved_sse = settings.sse_enabled
    settings.sse_enabled = False
    try:
        org_id, agent_id, sess_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with sessionmaker(sync_engine, expire_on_commit=False)() as db:
            db.add(Agent(id=agent_id, org_id=org_id, name="a", framework="generic"))
            db.add(
                SessionModel(
                    id=sess_id,
                    org_id=org_id,
                    agent_id=agent_id,
                    goal="g",
                    constraints=[],
                    expected_resources=[],
                    status="active",
                    drift_score=0.0,
                    evaluation_count=0,
                )
            )
            # A real chained row, as the intercept path would write it.
            intercept = log_event_sync(
                db,
                agent_id=agent_id,
                org_id=org_id,
                action_type="read",
                target_resource="/x",
                decision="allowed",
                policy_id=None,
                metadata={},
                session_id=sess_id,
            )
            db.commit()
            assert flag_failed_review(db, audit_event_id=intercept.id, session_id=sess_id, error_type="ValueError")

        async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

        async def verify():
            async with AsyncSession(async_engine) as s:
                result = await verify_org_chain(s, org_id=org_id)
            await async_engine.dispose()
            return result

        result = asyncio.run(verify())
        assert result.ok is True
        assert result.checked == 2
        assert result.first_bad_sequence is None
    finally:
        sync_engine.dispose()
        settings.sse_enabled = saved_sse
