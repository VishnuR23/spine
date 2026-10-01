from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from spine.config.settings import settings
from spine.core import redis_guard
from spine.core.redaction import stream_safe_audit_payload
from spine.schemas.events import SpineStreamEvent

logger = logging.getLogger(__name__)


def _channel(org_id: uuid.UUID) -> str:
    return f"spine:events:{org_id}"


def _encode(event_type: str, org_id: uuid.UUID, data: dict[str, Any]) -> str:
    payload = SpineStreamEvent(
        type=event_type,  # type: ignore[arg-type]
        org_id=org_id,
        ts=datetime.now(timezone.utc),
        data=data,
    )
    return payload.model_dump_json()


async def publish_event_async(org_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> None:
    # Runs inside API requests, including intercept: bounded and skipped
    # while Redis is down, so a live-feed update never delays an order check.
    if not settings.sse_enabled or not redis_guard.available():
        return
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True, **redis_guard.client_kwargs())
        try:
            await client.publish(_channel(org_id), _encode(event_type, org_id, data))
        finally:
            await client.aclose()
    except Exception:
        redis_guard.mark_down()
        logger.debug("event_bus publish failed", exc_info=True)


def publish_event_sync(org_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> None:
    if not settings.sse_enabled:
        return
    try:
        import redis

        client = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            client.publish(_channel(org_id), _encode(event_type, org_id, data))
        finally:
            client.close()
    except Exception:
        logger.debug("event_bus publish_sync failed", exc_info=True)


def audit_event_payload(event) -> dict[str, Any]:
    return stream_safe_audit_payload(event)


def approval_payload(
    *,
    approval_id: uuid.UUID,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    status: str,
    audit_event_id: uuid.UUID | None = None,
    action_type: str | None = None,
    target_resource: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(approval_id),
        "org_id": str(org_id),
        "agent_id": str(agent_id),
        "status": status,
        "audit_event_id": str(audit_event_id) if audit_event_id else None,
        "action_type": action_type,
        "target_resource": target_resource,
        "reason": reason,
    }


async def subscribe_org_events(org_id: uuid.UUID) -> AsyncIterator[SpineStreamEvent]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(_channel(org_id))
    try:
        while True:
            # 100ms poll: tightens "published → received" tail from ~1s to
            # ~100ms with negligible CPU. fd-poll only.
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is None:
                await asyncio.sleep(0.01)
                continue
            if message.get("type") != "message":
                continue
            raw = message.get("data")
            if not raw:
                continue
            try:
                yield SpineStreamEvent.model_validate_json(raw)
            except Exception:
                logger.debug("invalid stream event payload", exc_info=True)
    finally:
        await pubsub.unsubscribe(_channel(org_id))
        await pubsub.aclose()
        await client.aclose()
