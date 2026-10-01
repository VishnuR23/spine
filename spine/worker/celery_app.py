from celery import Celery

from spine.config.settings import settings

celery_app = Celery(
    "spine",
    broker=settings.effective_celery_broker_url,
    include=["spine.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=settings.celery_task_always_eager,
    # The API enqueues from inside order checks. Bound the broker connect so
    # an unreachable Redis costs milliseconds, not the OS connect timeout.
    # Only the connect is bounded: a read timeout here would also apply to
    # the worker's long-polling consumer.
    broker_connection_timeout=settings.redis_hot_path_timeout_ms / 1000.0,
    broker_transport_options={"socket_connect_timeout": settings.redis_hot_path_timeout_ms / 1000.0},
)
