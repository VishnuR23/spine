from __future__ import annotations

import logging
import uuid

from spine.db.sync_database import get_sync_session
from spine.monitor.plan_engine import evaluate_plan_alignment, flag_failed_review
from spine.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="spine.evaluate_plan_alignment", bind=True, max_retries=3)
def evaluate_plan_alignment_task(self, audit_event_id: str, session_id: str) -> dict | None:
    """Run the plan-bound reviewer for one (session, audit_event) pair."""
    db = get_sync_session()
    try:
        evaluation = evaluate_plan_alignment(
            db,
            audit_event_id=uuid.UUID(audit_event_id),
            session_id=uuid.UUID(session_id),
        )
        if evaluation is None:
            return None
        return {
            "plan_evaluation_id": str(evaluation.id),
            "alignment": evaluation.alignment,
            "drift_score_after": evaluation.drift_score_after,
            "approval_id": str(evaluation.approval_id) if evaluation.approval_id else None,
        }
    except Exception as exc:
        logger.exception(
            "Plan-bound evaluation failed for audit_event_id=%s session_id=%s",
            audit_event_id,
            session_id,
        )
        db.rollback()
        if self.request.retries >= self.max_retries:
            # Out of retries: fail closed. Put the action in front of a human
            # rather than letting it go unreviewed.
            try:
                flag_failed_review(
                    db,
                    audit_event_id=uuid.UUID(audit_event_id),
                    session_id=uuid.UUID(session_id),
                    error_type=type(exc).__name__,
                )
            except Exception:
                db.rollback()
                logger.exception(
                    "Could not flag failed review for audit_event_id=%s session_id=%s",
                    audit_event_id,
                    session_id,
                )
        raise self.retry(exc=exc, countdown=2**self.request.retries)
    finally:
        db.close()
