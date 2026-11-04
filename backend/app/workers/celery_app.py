"""Celery application (worker + beat). Broker/result backend come from REDIS_URL."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "mantiq_outreach",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    "discover-tick": {"task": "outreach.discover_tick", "schedule": crontab(minute="*/30")},
    "enrich-tick": {"task": "outreach.enrich_tick", "schedule": crontab(minute="*/10")},
    "research-tick": {"task": "outreach.research_tick", "schedule": crontab(minute="*/15")},
    "qualify-tick": {"task": "outreach.qualify_tick", "schedule": crontab(minute="*/15")},
    "plan-sequences": {"task": "outreach.plan_sequences", "schedule": crontab(minute="*/20")},
    "compose-due": {"task": "outreach.compose_due", "schedule": crontab(minute="*/5")},
    "send-due": {"task": "outreach.send_due", "schedule": crontab(minute="*")},
    "poll-imap": {"task": "outreach.poll_imap", "schedule": crontab(minute="*")},
    "handle-replies": {"task": "outreach.handle_replies", "schedule": crontab(minute="*/5")},
    "breaker-check": {"task": "outreach.breaker_check", "schedule": crontab(minute="*/5")},
    "warmup-advance": {"task": "outreach.warmup_advance", "schedule": crontab(hour=6, minute=0)},
    "daily-report": {"task": "outreach.daily_report", "schedule": crontab(hour=7, minute=0)},
    "retry-soft-bounces": {"task": "outreach.retry_soft_bounces", "schedule": crontab(minute="*/30")},
    "purge-retention": {"task": "outreach.purge_retention", "schedule": crontab(hour=3, minute=0)},
}
