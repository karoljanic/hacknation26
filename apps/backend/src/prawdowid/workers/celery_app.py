import os
from urllib.parse import quote

from celery import Celery
from kombu import Queue

user = quote(os.environ.get("RABBITMQ_USER", "guest"), safe="")
password = quote(os.environ.get("RABBITMQ_PASSWORD", "guest"), safe="")
host = os.environ.get("RABBITMQ_HOST", "localhost")

app = Celery("prawdowid", broker=f"amqp://{user}:{password}@{host}:5672//")
app.conf.task_queues = tuple(
    Queue(name)
    for name in ("default", "fetch", "claims", "search", "evidence", "aggregate", "ingestion")
)
app.conf.task_default_queue = "default"
app.conf.broker_connection_retry_on_startup = True
