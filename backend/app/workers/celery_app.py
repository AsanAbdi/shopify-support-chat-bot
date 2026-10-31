from celery import Celery
from celery.schedules import crontab

from app.config import get_settings
from app.logging_conf import setup_logging
from app.observability import init_sentry

settings = get_settings()
setup_logging()
init_sentry(settings)

celery = Celery("support", broker=settings.redis_url, backend=settings.redis_url, include=["app.workers.tasks"])
celery.conf.update(
    task_serializer="json", accept_content=["json"], result_expires=3600, timezone="Europe/Berlin",
    task_acks_late=True, worker_prefetch_multiplier=1, task_time_limit=900, broker_connection_retry_on_startup=True,
    beat_schedule={
        "catalog-sync-daily": {"task": "app.workers.tasks.sync_catalog_task", "schedule": crontab(hour=3, minute=0)},
        "url-docs-refresh-daily": {"task": "app.workers.tasks.refresh_url_documents", "schedule": crontab(hour=3, minute=30)},
        "retention-daily": {"task": "app.workers.tasks.retention_task", "schedule": crontab(hour=4, minute=0)},
    },
)
