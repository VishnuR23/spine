"""Point the app's Redis users at a given URL for one test."""

from spine.config.settings import settings
from spine.core import redis_guard
from spine.worker.celery_app import celery_app


def point_redis_at(monkeypatch, url: str) -> None:
    monkeypatch.setattr(settings, "redis_url", url)
    monkeypatch.setattr(celery_app.conf, "broker_url", url)
    # Celery builds its connection and producer pools once, from the broker
    # URL at first use. Drop them so the next enqueue connects to `url`.
    if celery_app._pool is not None:
        celery_app._pool.force_close_all()
        celery_app._pool = None
    celery_app.amqp._producer_pool = None
    redis_guard.reset()
