"""Plan-bound reviewer engine.

Runs in the Celery worker. For each (session, audit_event) pair:

  1. Loads the session plan (Spine-owned).
  2. Loads the current audit event (uses ONLY action_type + target_resource).
  3. Builds an isolated context using prior plan_evaluations as history.
  4. Calls Claude with a strict JSON-only prompt.
  5. Writes a plan_evaluation row.
  6. Updates the session's drift_score via exponential moving average.
  7. Creates an approval ticket + dispatches webhook + publishes SSE if the
     verdict is divergent OR the new drift_score crosses the flag threshold.

The reviewer prompt builder is in spine.monitor.prompts.plan_alignment and
is the structural moat against prompt injection -- see that module's docstring.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session as DbSession

from spine.config.settings import settings
from spine.core.sync_audit_logger import log_event_sync
from spine.models.approval import Approval
from spine.models.audit_event import AuditEvent
from spine.models.plan_evaluation import PlanEvaluation
from spine.models.session import Session
from spine.monitor.prompts.plan_alignment import (
    SYSTEM_PROMPT,
    build_plan_user_message,
)
from spine.observability.metrics import (
    inc_monitor_evaluation,
    spine_plan_eval_duration_seconds,
)
from spine.schemas.session import PlanVerdict

logger = logging.getLogger(__name__)

# EMA alpha — recent verdicts weighted more heavily.
_EMA_ALPHA = 0.3


# ---------------------------------------------------------------------------
# Helpers (overridable for tests)
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object in reviewer response")
    return json.loads(match.group(0))


def _call_reviewer(system_prompt: str, user_message: str) -> str:
    """Call Claude. Isolated so tests can monkeypatch this."""
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.monitor_model,
        # Current models think before answering, and thinking counts against
        # max_tokens. A tight cap can use up the budget before the JSON verdict.
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return _reviewer_text(response)


def _reviewer_text(response) -> str:
    # A declined or truncated turn has no complete verdict. Say why, rather
    # than letting it surface later as a generic JSON parse failure.
    if response.stop_reason in ("refusal", "max_tokens"):
        raise ValueError(f"Reviewer returned no verdict (stop_reason={response.stop_reason})")
    text_blocks = [b.text for b in response.content if hasattr(b, "text") and b.text]
    return "\n".join(text_blocks)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _load_recent_history(db: DbSession, *, session_id: uuid.UUID, limit: int) -> list[dict]:
    """Summarize the last N plan_evaluations in this session for the reviewer.

    Joins to audit_events for action_type + target_resource. Returns only
    safe primitives -- never metadata, never tool outputs.
    """
    rows = db.execute(
        sa.select(
            PlanEvaluation.alignment,
            AuditEvent.action_type,
            AuditEvent.target_resource,
        )
        .join(AuditEvent, AuditEvent.id == PlanEvaluation.audit_event_id)
        .where(PlanEvaluation.session_id == session_id)
        .order_by(PlanEvaluation.created_at.desc())
        .limit(limit)
    ).all()
    # Reverse so oldest first.
    rows = list(reversed(rows))
    return [
        {
            "action_type": str(action_type or ""),
            "target_resource": str(target_resource or ""),
            "alignment": str(alignment or ""),
        }
        for alignment, action_type, target_resource in rows
    ]


def _next_drift_score(prev: float, contribution: float) -> float:
    new = _EMA_ALPHA * float(contribution) + (1.0 - _EMA_ALPHA) * float(prev)
    return max(0.0, min(1.0, new))


def _build_approval_proposed(*, agent_id: uuid.UUID, audit: AuditEvent, alignment: str) -> dict:
    meta = dict(audit.metadata_ or {})
    spine_meta = meta.setdefault("spine", {})
    if not isinstance(spine_meta, dict):
        spine_meta = {}
        meta["spine"] = spine_meta
    spine_meta["plan_alignment"] = alignment
    spine_meta["origin"] = "plan_drift"
    return {
        "agent_id": str(agent_id),
        "action": {
            "action_type": audit.action_type,
            "target_resource": audit.target_resource,
            "metadata": meta,
        },
        "correlation": None,
    }


def evaluate_plan_alignment(
    db: DbSession, *, audit_event_id: uuid.UUID, session_id: uuid.UUID
) -> PlanEvaluation | None:
    """Run the plan-bound reviewer for one (session, audit_event) pair.

    Returns the persisted PlanEvaluation, or None if the evaluation was
    skipped (session inactive / missing / already evaluated).
    """
    t0 = time.perf_counter()
    try:
        return _evaluate_plan_alignment_inner(db, audit_event_id=audit_event_id, session_id=session_id)
    finally:
        spine_plan_eval_duration_seconds.observe(time.perf_counter() - t0)


def _evaluate_plan_alignment_inner(
    db: DbSession, *, audit_event_id: uuid.UUID, session_id: uuid.UUID
) -> PlanEvaluation | None:
    sess = db.get(Session, session_id)
    if sess is None or sess.status != "active":
        return None

    audit = db.get(AuditEvent, audit_event_id)
    if audit is None or audit.org_id != sess.org_id:
        return None
    # We don't re-evaluate intercepts that were blocked by the policy engine
    # before plan-bound monitoring -- they never actually ran. Saves Claude
    # calls and avoids confusing drift signal.
    if audit.policy_decision == "blocked" and settings.monitor_skip_if_policy_blocked:
        return None

    # Idempotency: if a verdict already exists for this pair, return it.
    existing = db.execute(
        sa.select(PlanEvaluation)
        .where(PlanEvaluation.session_id == session_id)
        .where(PlanEvaluation.audit_event_id == audit_event_id)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Serialize session-level mutations to keep drift_score consistent under
    # concurrent intercepts in the same session.
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(k=str(session_id)))

    history = _load_recent_history(db, session_id=session_id, limit=settings.plan_eval_history_window)

    user_message = build_plan_user_message(
        goal=sess.goal,
        constraints=list(sess.constraints or []),
        expected_resources=list(sess.expected_resources or []),
        success_criteria=sess.success_criteria,
        current_action_type=audit.action_type,
        current_target_resource=audit.target_resource,
        recent_history=history,
    )

    raw = _call_reviewer(SYSTEM_PROMPT, user_message)
    data = _extract_json(raw)
    verdict = PlanVerdict.model_validate(data)

    new_drift = _next_drift_score(sess.drift_score, verdict.drift_contribution)

    evaluation = PlanEvaluation(
        session_id=session_id,
        audit_event_id=audit_event_id,
        agent_id=sess.agent_id,
        org_id=sess.org_id,
        alignment=verdict.alignment,
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        drift_contribution=verdict.drift_contribution,
        drift_score_after=new_drift,
        model_id=settings.monitor_model,
    )
    db.add(evaluation)

    sess.drift_score = new_drift
    sess.evaluation_count = (sess.evaluation_count or 0) + 1

    db.flush()  # so evaluation.id is populated for the audit metadata below

    # Mirror existing monitor pattern: record the reviewer's verdict as an
    # audit row in the tamper-evident chain.
    log_event_sync(
        db,
        agent_id=sess.agent_id,
        org_id=sess.org_id,
        action_type="plan.evaluation",
        target_resource=str(audit.id),
        decision={
            "aligned": "allowed",
            "drifted": "flagged",
            "divergent": "blocked",
        }.get(verdict.alignment, "flagged"),
        policy_id=None,
        metadata={
            "plan_evaluation_id": str(evaluation.id),
            "session_id": str(session_id),
            "audit_event_id": str(audit_event_id),
            "alignment": verdict.alignment,
            "confidence": verdict.confidence,
            "drift_contribution": verdict.drift_contribution,
            "drift_score_after": new_drift,
            "reasoning_summary": verdict.reasoning[:500],
        },
        session_id=session_id,
    )
    inc_monitor_evaluation(recommendation=verdict.alignment, monitor_type="plan_alignment")

    # Decide whether to create an approval ticket. Two paths:
    #   - alignment == divergent (single divergent action is enough)
    #   - drift_score crossed the flag threshold (accumulated drift)
    should_flag = verdict.alignment == "divergent" or new_drift >= settings.plan_drift_flag_threshold
    approval_id: uuid.UUID | None = None
    if should_flag:
        # Avoid duplicate pending approvals on the same audit_event.
        existing_approval = db.execute(
            sa.select(Approval.id)
            .where(Approval.audit_event_id == audit_event_id)
            .where(Approval.status == "pending")
            .limit(1)
        ).scalar_one_or_none()
        if existing_approval is None:
            approval = Approval(
                org_id=sess.org_id,
                agent_id=sess.agent_id,
                proposed=_build_approval_proposed(
                    agent_id=sess.agent_id,
                    audit=audit,
                    alignment=verdict.alignment,
                ),
                status="pending",
                audit_event_id=audit_event_id,
            )
            db.add(approval)
            db.flush()
            approval_id = approval.id
            evaluation.approval_id = approval_id
        else:
            approval_id = existing_approval

    db.commit()
    db.refresh(evaluation)
    db.refresh(sess)

    # SSE: publish the plan_evaluation event for the dashboard.
    from spine.core.event_bus import publish_event_sync
    from spine.core.redaction import stream_safe_plan_payload

    publish_event_sync(
        sess.org_id,
        "plan_evaluation",
        stream_safe_plan_payload(evaluation=evaluation, drift_score=new_drift, session_id=session_id),
    )

    # Drift webhook + approval SSE only when we actually flagged.
    if should_flag and approval_id is not None:
        from spine.core.event_bus import approval_payload
        from spine.core.webhook_dispatch import dispatch_plan_drift_event_sync

        dispatch_plan_drift_event_sync(
            db,
            org_id=sess.org_id,
            payload={
                "event": "spine.plan.drift",
                "session_id": str(session_id),
                "agent_id": str(sess.agent_id),
                "org_id": str(sess.org_id),
                "drift_score": new_drift,
                "last_action": {
                    "action_type": audit.action_type,
                    "target_resource": audit.target_resource,
                    "alignment": verdict.alignment,
                    "reasoning": verdict.reasoning[:500],
                },
                "approval_id": str(approval_id),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        publish_event_sync(
            sess.org_id,
            "approval",
            approval_payload(
                approval_id=approval_id,
                org_id=sess.org_id,
                agent_id=sess.agent_id,
                status="pending",
                audit_event_id=audit_event_id,
                action_type=audit.action_type,
                target_resource=audit.target_resource,
                reason=f"plan {verdict.alignment} (drift {new_drift:.2f})",
            ),
        )

    return evaluation
