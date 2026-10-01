"""Redis-backed cache for an org's active policies.

The intercept hot path needs sub-50ms p99. Loading policies from Postgres on
every intercept costs a roundtrip. This module caches the full set of policies
for an org in Redis and filters in-memory.

Invalidation is explicit: any CRUD on policies in /v1/policies routes calls
:func:`invalidate` for the affected org. Cache also carries a TTL as a safety
net in case an invalidation is dropped.

Fail-safe: if Redis is unreachable or disabled, every call transparently
falls through to a DB read — correctness is preserved, only the speed benefit
is lost.
"""

from __future__ import annotations

import json
import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from spine.config.settings import settings
from spine.core import redis_guard
from spine.models.policy import Policy

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300  # 5 min safety net; explicit invalidation is the primary mechanism


def _key(org_id: uuid.UUID) -> str:
    return f"spine:policies:cache:{org_id}"


async def _aredis(*, hot_path: bool = False):
    """Return an asyncio redis client or None.

    hot_path clients are bounded by redis_guard's timeouts, and are not
    created at all while Redis is being skipped after a failure.
    """
    if not settings.redis_url.strip():
        return None
    if hot_path and not redis_guard.available():
        return None
    try:
        import redis.asyncio as aioredis

        extra = redis_guard.client_kwargs() if hot_path else {}
        return aioredis.from_url(settings.redis_url, decode_responses=True, **extra)
    except Exception:
        logger.debug("redis async unavailable", exc_info=True)
        return None


def _sredis():
    if not settings.redis_url.strip():
        return None
    try:
        import redis

        return redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        logger.debug("redis sync unavailable", exc_info=True)
        return None


def _serialize(p: Policy) -> dict:
    return {
        "id": str(p.id),
        "agent_id": str(p.agent_id) if p.agent_id else None,
        "name": p.name,
        "rule_type": p.rule_type,
        "rule_config": p.rule_config or {},
        "is_active": bool(p.is_active),
    }


async def _load_from_db(db: AsyncSession, *, org_id: uuid.UUID) -> list[dict]:
    result = await db.execute(sa.select(Policy).where(Policy.org_id == org_id))
    return [_serialize(p) for p in result.scalars().all()]


def _filter_for_agent(policies: list[dict], *, agent_id: uuid.UUID) -> list[dict]:
    aid = str(agent_id)
    return [p for p in policies if p["is_active"] and (p["agent_id"] is None or p["agent_id"] == aid)]


async def get_active_policies_for_intercept(db: AsyncSession, *, org_id: uuid.UUID, agent_id: uuid.UUID) -> list[dict]:
    """Hot-path read used by intercept_service.

    Returns the org's active policies that apply to this agent (org-wide or
    agent-scoped). Tries Redis first; falls through to Postgres on miss.
    """
    from spine.observability.metrics import inc_policy_cache_event

    client = await _aredis(hot_path=True)
    if client is not None:
        try:
            raw = await client.get(_key(org_id))
            if raw:
                inc_policy_cache_event("hit")
                return _filter_for_agent(json.loads(raw), agent_id=agent_id)
            inc_policy_cache_event("miss")
        except Exception:
            inc_policy_cache_event("error")
            redis_guard.mark_down()
            logger.debug("policy cache read failed", exc_info=True)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
    else:
        inc_policy_cache_event("bypass")

    policies = await _load_from_db(db, org_id=org_id)

    # Repopulate cache (best-effort)
    repopulate = await _aredis(hot_path=True)
    if repopulate is not None:
        try:
            await repopulate.setex(_key(org_id), CACHE_TTL_SECONDS, json.dumps(policies, default=str))
        except Exception:
            redis_guard.mark_down()
            logger.debug("policy cache write failed", exc_info=True)
        finally:
            try:
                await repopulate.aclose()
            except Exception:
                pass

    return _filter_for_agent(policies, agent_id=agent_id)


async def invalidate(org_id: uuid.UUID) -> None:
    """Drop the cached entry for one org. Call after any policy CRUD."""
    client = await _aredis()
    if client is None:
        return
    try:
        await client.delete(_key(org_id))
    except Exception:
        logger.debug("policy cache invalidate failed", exc_info=True)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def invalidate_sync(org_id: uuid.UUID) -> None:
    """Sync variant for Celery workers etc."""
    client = _sredis()
    if client is None:
        return
    try:
        client.delete(_key(org_id))
    except Exception:
        logger.debug("policy cache invalidate_sync failed", exc_info=True)
    finally:
        try:
            client.close()
        except Exception:
            pass
