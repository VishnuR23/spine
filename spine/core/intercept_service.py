import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from spine.config.settings import settings
from spine.core import redis_guard
from spine.core.approval_grants import find_active_grant
from spine.core.audit_logger import log_event
from spine.core.identity import get_active_agent
from spine.core.policy_engine import decide, match_policy
from spine.core.webhook_dispatch import dispatch_intercept_event
from spine.models.approval import Approval
from spine.models.session import Session as SessionModel
from spine.observability.metrics import inc_intercept
from spine.schemas.intercept import InterceptRequest, InterceptResponse

logger = logging.getLogger(__name__)


async def _load_active_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> SessionModel | None:
    """Load a session if it exists, is active, and belongs to this (org, agent)."""
    result = await db.execute(
        sa.select(SessionModel)
        .where(SessionModel.id == session_id)
        .where(SessionModel.org_id == org_id)
        .where(SessionModel.agent_id == agent_id)
        .where(SessionModel.status == "active")
        .limit(1)
    )
    return result.scalar_one_or_none()


async def run_intercept(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    request_id: str,
    payload: InterceptRequest,
    record_intercept_metrics: bool = True,
) -> InterceptResponse:
    agent = await get_active_agent(db, payload.agent_id)
    if not agent or agent.org_id != org_id:
        # keep message generic
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # ------------------------------------------------------------------
    # Plan-bound monitoring: if the intercept carries a session_id, load
    # the session and apply the pre-policy drift gate. If drift has crossed
    # the block threshold, short-circuit with a hard block before the policy
    # engine even runs.
    # ------------------------------------------------------------------
    session_obj: SessionModel | None = None
    if payload.session_id is not None:
        session_obj = await _load_active_session(db, session_id=payload.session_id, org_id=org_id, agent_id=agent.id)
        if session_obj is None:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or inactive",
            )

    drift_blocked = session_obj is not None and session_obj.drift_score >= settings.plan_drift_block_threshold

    if drift_blocked:
        decision = "blocked"
        allowed = False
        reason = (
            f"Session drift threshold exceeded ({session_obj.drift_score:.2f} >= "
            f"{settings.plan_drift_block_threshold:.2f})"
        )
        matched = []
        policy_id = None
        base_meta = dict(payload.action.metadata or {})
        if payload.correlation:
            corr = payload.correlation.model_dump(exclude_none=True)
            if corr:
                base_meta.setdefault("spine", {})
                if not isinstance(base_meta["spine"], dict):
                    base_meta["spine"] = {"value": base_meta["spine"]}
                base_meta["spine"].update({"correlation": corr, "request_id": request_id})
        spine_meta = base_meta.setdefault("spine", {})
        if not isinstance(spine_meta, dict):
            spine_meta = {}
            base_meta["spine"] = spine_meta
        spine_meta["drift_block"] = True
        spine_meta["drift_score"] = session_obj.drift_score

        audit = await log_event(
            db,
            agent_id=agent.id,
            org_id=agent.org_id,
            action_type=payload.action.action_type,
            target_resource=payload.action.target_resource,
            decision=decision,
            policy_id=None,
            metadata=base_meta,
            session_id=payload.session_id,
        )

        await dispatch_intercept_event(
            db,
            org_id=agent.org_id,
            payload={
                "type": "spine.intercept",
                "org_id": str(agent.org_id),
                "agent_id": str(agent.id),
                "decision": decision,
                "allowed": allowed,
                "reason": reason,
                "audit_event_id": str(audit.id),
                "request_id": request_id,
                "session_id": str(payload.session_id),
                "drift_score": session_obj.drift_score,
            },
        )

        if record_intercept_metrics:
            inc_intercept(decision=decision, allowed=allowed)

        return InterceptResponse(
            allowed=allowed,
            decision=decision,
            reason=reason,
            audit_event_id=audit.id,
            request_id=request_id,
            approval_id=None,
            session_id=payload.session_id,
            drift_score=session_obj.drift_score,
        )

    # ------------------------------------------------------------------
    # Normal policy evaluation path.
    # ------------------------------------------------------------------
    # Read through Redis cache when available; falls through to Postgres
    # on cache miss or Redis-down.
    from spine.core.policy_cache import get_active_policies_for_intercept

    policies_data = await get_active_policies_for_intercept(db, org_id=agent.org_id, agent_id=agent.id)

    matched = []
    matched_with_ids: list[tuple[object, uuid.UUID]] = []
    for p in policies_data:
        m = match_policy(p["name"], p["rule_config"], payload.action)
        if m:
            matched.append(m)
            matched_with_ids.append((m, uuid.UUID(p["id"])))

    decision, allowed, reason = decide(matched)

    policy_id: uuid.UUID | None = None
    if matched_with_ids:
        for m, pid in matched_with_ids:
            if getattr(m, "reason", None) == reason:
                policy_id = pid
                break
        policy_id = policy_id or matched_with_ids[0][1]

    base_meta = dict(payload.action.metadata or {})
    if payload.correlation:
        corr = payload.correlation.model_dump(exclude_none=True)
        if corr:
            base_meta.setdefault("spine", {})
            if not isinstance(base_meta["spine"], dict):
                base_meta["spine"] = {"value": base_meta["spine"]}
            base_meta["spine"].update({"correlation": corr, "request_id": request_id})

    grant = await find_active_grant(db, org_id=agent.org_id, agent_id=agent.id, action=payload.action)
    if grant and (not allowed or decision == "flagged"):
        decision = "allowed"
        allowed = True
        reason = f"Human approval ({grant.decided_by})"
        spine_meta = base_meta.setdefault("spine", {})
        if not isinstance(spine_meta, dict):
            spine_meta = {}
            base_meta["spine"] = spine_meta
        spine_meta["approval_grant_id"] = str(grant.id)
        spine_meta["approval_id"] = str(grant.approval_id)
        policy_id = None

    audit = await log_event(
        db,
        agent_id=agent.id,
        org_id=agent.org_id,
        action_type=payload.action.action_type,
        target_resource=payload.action.target_resource,
        decision=decision,
        policy_id=policy_id,
        metadata=base_meta,
        session_id=payload.session_id,
    )

    approval_id: uuid.UUID | None = None
    if decision == "flagged":
        approval = Approval(
            org_id=agent.org_id,
            agent_id=agent.id,
            proposed=payload.model_dump(mode="json"),
            status="pending",
            audit_event_id=audit.id,
        )
        db.add(approval)
        await db.commit()
        await db.refresh(approval)
        approval_id = approval.id

        from spine.core.event_bus import approval_payload, publish_event_async

        await publish_event_async(
            agent.org_id,
            "approval",
            approval_payload(
                approval_id=approval.id,
                org_id=agent.org_id,
                agent_id=agent.id,
                status="pending",
                audit_event_id=audit.id,
                action_type=payload.action.action_type,
                target_resource=payload.action.target_resource,
                reason=reason,
            ),
        )

    # Best-effort webhook delivery (do not break intercept if webhooks fail)
    webhook_payload: dict = {
        "type": "spine.intercept",
        "org_id": str(agent.org_id),
        "agent_id": str(agent.id),
        "decision": decision,
        "allowed": allowed,
        "reason": reason,
        "audit_event_id": str(audit.id),
        "request_id": request_id,
    }
    if payload.session_id is not None:
        webhook_payload["session_id"] = str(payload.session_id)
        webhook_payload["drift_score"] = session_obj.drift_score if session_obj else 0.0
    await dispatch_intercept_event(db, org_id=agent.org_id, payload=webhook_payload)

    if record_intercept_metrics:
        inc_intercept(decision=decision, allowed=allowed)

    # Plan-bound monitoring: enqueue a plan_alignment evaluation for any
    # intercept that carried a session_id and was not policy-blocked.
    #
    # The enqueue is a blocking broker call inside this async handler. With
    # Celery's default publish retries it took ~650 ms when the broker was
    # down, long enough to expire an order's latency budget. So: no retries,
    # a bounded connect (celery_app), and skipped while Redis is down. An
    # enqueue that fails here means that order goes unreviewed.
    if payload.session_id is not None and decision != "blocked" and redis_guard.available():
        try:
            from spine.worker.tasks import evaluate_plan_alignment_task

            evaluate_plan_alignment_task.apply_async((str(audit.id), str(payload.session_id)), retry=False)
        except Exception:
            redis_guard.mark_down()
            logger.warning("plan review enqueue failed; this order will not be reviewed", exc_info=True)

    return InterceptResponse(
        allowed=allowed,
        decision=decision,
        reason=reason,
        audit_event_id=audit.id,
        request_id=request_id,
        approval_id=approval_id,
        session_id=payload.session_id,
        drift_score=session_obj.drift_score if session_obj else None,
    )
