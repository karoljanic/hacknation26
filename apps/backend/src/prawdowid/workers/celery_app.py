import os
from urllib.parse import quote

from celery import Celery
from kombu import Exchange, Queue

user = quote(os.environ.get("RABBITMQ_USER", "guest"), safe="")
password = quote(os.environ.get("RABBITMQ_PASSWORD", "guest"), safe="")
host = os.environ.get("RABBITMQ_HOST", "localhost")

QUEUES = ("default", "fetch", "claims", "search", "evidence", "aggregate", "ingestion", "embedding")

app = Celery(
    "prawdowid",
    broker=f"amqp://{user}:{password}@{host}:5672//",
    include=["prawdowid.workers.tasks"],
)
# Each queue gets its own direct exchange + routing key. (A bare Queue(name) is
# bound to the shared "default" exchange with key "default", so one message
# would be copied into every queue.)
app.conf.task_queues = tuple(Queue(name, Exchange(name, type="direct"), routing_key=name) for name in QUEUES)
app.conf.task_default_queue = "default"
app.conf.task_default_exchange = "default"
app.conf.task_default_routing_key = "default"
# Route by task-name prefix, so senders that only know the name (send_task,
# `celery call`, other services) reach the right queue.
app.conf.task_routes = {
    "ingestion.*": {"queue": "ingestion"},
    "embedding.*": {"queue": "embedding"},
}
app.conf.broker_connection_retry_on_startup = True
