import logging

from . import jobs
from .config import settings
from .db import init_db

logging.getLogger().setLevel(logging.INFO)

if settings.auto_create_tables:
    init_db()


# Lambda entrypoint for async jobs and the nightly schedule: app.worker.handler
def handler(event, context):
    jobs.handle(event)
    return {"ok": True}
