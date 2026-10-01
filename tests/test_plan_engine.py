"""Plan engine: EMA arithmetic, idempotency, divergent → approval, mocked Claude."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spine.config.settings import settings
from spine.db.base import Base
from spine.models.agent import Agent
from spine.models.approval import Approval
from spine.models.audit_event import AuditEvent
from spine.models.plan_evaluation import PlanEvaluation
from spine.models.session import Session as SessionModel
from spine.monitor.plan_engine import (
    _next_drift_score,
    evaluate_plan_alignment,
)


@pytest.fixture()
def db():
    saved_sse = settings.sse_enabled
    saved_skip = settings.monitor_skip_if_policy_blocked
    settings.sse_enabled = False
    settings.monitor_skip_if_policy_blocked = True
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
        settings.sse_enabled = saved_sse
        settings.monitor_skip_if_policy_blocked = saved_skip


def _seed(db, *, alignment_default="aligned"):
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    sess_id = uuid.uuid4()
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
    db.commit()
    return org_id, agent_id, sess_id


def _add_audit(db, *, org_id, agent_id, session_id=None, decision="allowed"):
    e = AuditEvent(
        agent_id=agent_id,
        org_id=org_id,
        action_type="read",
        target_resource="/x",
        policy_decision=decision,
        sequence=1,
        prev_hash=None,
        event_hash="x" * 64,
        session_id=session_id,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def test_ema_arithmetic_increases_with_high_drift():
    # 0.3 * 0.8 + 0.7 * 0.0 = 0.24
    assert abs(_next_drift_score(0.0, 0.8) - 0.24) < 1e-9
    # 0.3 * 1.0 + 0.7 * 0.24 = 0.468
    assert abs(_next_drift_score(0.24, 1.0) - 0.468) < 1e-9


def test_ema_decays_with_aligned_actions():
    # high drift, then aligned: should drop
    after = _next_drift_score(0.6, 0.0)
    assert after < 0.6


def test_aligned_verdict_does_not_create_approval(monkeypatch, db):
    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    def fake_call(_sys, _user):
        return '{"alignment": "aligned", "confidence": 0.9, "reasoning": "stays on plan", "drift_contribution": 0.0}'

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", fake_call)

    ev = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    assert ev is not None
    assert ev.alignment == "aligned"
    assert ev.approval_id is None
    sess = db.get(SessionModel, sess_id)
    assert sess.drift_score == 0.0
    assert sess.evaluation_count == 1
    # No approvals were created
    assert db.query(Approval).count() == 0


def test_divergent_verdict_creates_approval(monkeypatch, db):
    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    def fake_call(_sys, _user):
        return (
            '{"alignment": "divergent", "confidence": 0.95, '
            '"reasoning": "reading secrets is not on plan", "drift_contribution": 0.9}'
        )

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", fake_call)

    ev = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    assert ev is not None
    assert ev.alignment == "divergent"
    assert ev.approval_id is not None
    approvals = db.query(Approval).all()
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].audit_event_id == audit.id


def test_idempotency_returns_existing(monkeypatch, db):
    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    calls = {"n": 0}

    def fake_call(_sys, _user):
        calls["n"] += 1
        return '{"alignment": "aligned", "confidence": 0.9, "reasoning": "ok", "drift_contribution": 0.0}'

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", fake_call)

    ev1 = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    ev2 = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    assert ev1.id == ev2.id
    assert calls["n"] == 1  # second call short-circuits before invoking the reviewer
    assert db.query(PlanEvaluation).count() == 1


def test_inactive_session_skipped(monkeypatch, db):
    org_id, agent_id, sess_id = _seed(db)
    sess = db.get(SessionModel, sess_id)
    sess.status = "completed"
    db.commit()
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    def fake_call(_sys, _user):
        return '{"alignment": "aligned", "confidence": 0.9, "reasoning": "x", "drift_contribution": 0.0}'

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", fake_call)

    ev = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    assert ev is None
    assert db.query(PlanEvaluation).count() == 0


def test_blocked_audit_event_skipped_when_setting_enabled(monkeypatch, db):
    settings.monitor_skip_if_policy_blocked = True
    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id, decision="blocked")

    def fake_call(_sys, _user):
        return '{"alignment": "aligned", "confidence": 0.9, "reasoning": "x", "drift_contribution": 0.0}'

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", fake_call)

    ev = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    assert ev is None


def test_accumulated_drift_creates_approval_above_flag_threshold(monkeypatch, db):
    """Even if no single action is divergent, accumulated drift past the
    flag threshold opens an approval ticket."""
    org_id, agent_id, sess_id = _seed(db)
    # Bump baseline drift_score to just below the flag threshold.
    sess = db.get(SessionModel, sess_id)
    sess.drift_score = settings.plan_drift_flag_threshold - 0.05
    db.commit()

    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    # Drifted (not divergent) verdict with a contribution that pushes drift
    # over the flag threshold via EMA.
    def fake_call(_sys, _user):
        return '{"alignment": "drifted", "confidence": 0.8, "reasoning": "borderline", "drift_contribution": 0.8}'

    monkeypatch.setattr("spine.monitor.plan_engine._call_reviewer", fake_call)

    ev = evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)
    assert ev is not None
    assert ev.alignment == "drifted"
    assert ev.drift_score_after >= settings.plan_drift_flag_threshold
    # Should have created an approval ticket.
    assert ev.approval_id is not None


def _fake_response(stop_reason: str, *texts: str):
    from types import SimpleNamespace

    blocks = [SimpleNamespace(type="text", text=t) for t in texts]
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


def test_reviewer_text_joins_text_blocks():
    from spine.monitor.plan_engine import _reviewer_text

    assert _reviewer_text(_fake_response("end_turn", '{"a":', " 1}")) == '{"a":\n 1}'


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_reviewer_text_names_the_stop_reason(stop_reason):
    from spine.monitor.plan_engine import _reviewer_text

    with pytest.raises(ValueError, match=stop_reason):
        _reviewer_text(_fake_response(stop_reason))


# ---------------------------------------------------------------------------
# Reviewer failure: fail closed by flagging the action for a human
# ---------------------------------------------------------------------------


def test_failed_review_opens_approval_without_touching_drift(db):
    from spine.monitor.plan_engine import flag_failed_review

    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    approval_id = flag_failed_review(db, audit_event_id=audit.id, session_id=sess_id, error_type="ValueError")

    assert approval_id is not None
    approval = db.get(Approval, approval_id)
    assert approval.status == "pending"
    assert approval.audit_event_id == audit.id
    assert approval.proposed["action"]["metadata"]["spine"]["origin"] == "plan_review_failed"

    row = db.query(AuditEvent).filter(AuditEvent.action_type == "plan.evaluation_failed").one()
    assert row.policy_decision == "flagged"
    assert row.target_resource == str(audit.id)
    assert row.session_id == sess_id
    assert row.metadata_["error_type"] == "ValueError"
    assert row.metadata_["approval_id"] == str(approval_id)

    # No verdict was produced, so nothing that depends on one moves.
    sess = db.get(SessionModel, sess_id)
    assert sess.drift_score == 0.0
    assert sess.evaluation_count == 0
    assert db.query(PlanEvaluation).count() == 0


def test_failed_review_is_idempotent(db):
    from spine.monitor.plan_engine import flag_failed_review

    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    first = flag_failed_review(db, audit_event_id=audit.id, session_id=sess_id, error_type="ValueError")
    second = flag_failed_review(db, audit_event_id=audit.id, session_id=sess_id, error_type="ValueError")

    assert first == second
    assert db.query(Approval).count() == 1
    assert db.query(AuditEvent).filter(AuditEvent.action_type == "plan.evaluation_failed").count() == 1


def test_failed_review_skipped_when_a_verdict_exists(monkeypatch, db):
    from spine.monitor.plan_engine import flag_failed_review

    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)
    monkeypatch.setattr(
        "spine.monitor.plan_engine._call_reviewer",
        lambda _s, _u: '{"alignment": "aligned", "confidence": 0.9, "reasoning": "ok", "drift_contribution": 0.0}',
    )
    evaluate_plan_alignment(db, audit_event_id=audit.id, session_id=sess_id)

    assert flag_failed_review(db, audit_event_id=audit.id, session_id=sess_id, error_type="ValueError") is None
    assert db.query(Approval).count() == 0


def test_failed_review_skipped_for_inactive_session(db):
    from spine.monitor.plan_engine import flag_failed_review

    org_id, agent_id, sess_id = _seed(db)
    db.get(SessionModel, sess_id).status = "completed"
    db.commit()
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id)

    assert flag_failed_review(db, audit_event_id=audit.id, session_id=sess_id, error_type="ValueError") is None
    assert db.query(Approval).count() == 0


def test_failed_review_skipped_for_policy_blocked_event(db):
    from spine.monitor.plan_engine import flag_failed_review

    settings.monitor_skip_if_policy_blocked = True
    org_id, agent_id, sess_id = _seed(db)
    audit = _add_audit(db, org_id=org_id, agent_id=agent_id, session_id=sess_id, decision="blocked")

    assert flag_failed_review(db, audit_event_id=audit.id, session_id=sess_id, error_type="ValueError") is None
    assert db.query(Approval).count() == 0
