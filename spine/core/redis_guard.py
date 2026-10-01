"""Bounded, self-skipping Redis access for the intercept hot path.

Redis backs three best-effort things on the order path: the policy cache, the
live event stream, and the reviewer's task queue. Each has a correct fallback
(Postgres, no live update, no review), so none of them may make an order check
wait. Two rules enforce that:

- Every hot-path Redis call has a short connect and read timeout, so an
  unreachable Redis costs at most that long instead of the OS connect timeout.
- After one failure, hot-path callers skip Redis entirely for a few seconds,
  so later orders do not each pay the timeout again.

The "Redis is down" flag is process-local by necessity: whether Redis is
reachable cannot itself be stored in Redis.

Admin-path calls (policy cache invalidation) deliberately do not use this.
A skipped or timed-out invalidation leaves a stale cache, which can leave a
newly restricted symbol unenforced until the TTL expires.
"""

from __future__ import annotations

import time

from spine.config.settings import settings

_down_until = 0.0


def client_kwargs() -> dict:
    """Timeouts for a hot-path redis client."""
    seconds = settings.redis_hot_path_timeout_ms / 1000.0
    return {"socket_connect_timeout": seconds, "socket_timeout": seconds}


def available() -> bool:
    """False while Redis is being skipped after a recent failure."""
    return time.monotonic() >= _down_until


def mark_down() -> None:
    """Skip Redis on the hot path for REDIS_RETRY_AFTER_SECONDS."""
    global _down_until
    _down_until = time.monotonic() + settings.redis_retry_after_seconds


def reset() -> None:
    global _down_until
    _down_until = 0.0
