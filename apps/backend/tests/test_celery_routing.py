"""Queue routing (no broker needed)."""

import pytest

import prawdowid.workers.tasks  # noqa: F401  (registers tasks)
from prawdowid.workers.celery_app import QUEUES, app


@pytest.mark.parametrize(
    ("task", "queue"),
    [
        ("ingestion.ingest_source", "ingestion"),
        ("ingestion.ingest_all", "ingestion"),
        ("embedding.embed_pending", "embedding"),
        ("something.else", "default"),
    ],
)
def test_tasks_route_by_name_even_without_import(task: str, queue: str) -> None:
    route = app.amqp.router.route({}, task)
    assert route["queue"].name == queue
    assert route["queue"].routing_key == queue


def test_each_queue_has_its_own_binding() -> None:
    queues = {q.name: q for q in app.conf.task_queues}
    assert set(queues) == set(QUEUES)
    assert len({(q.exchange.name, q.routing_key) for q in queues.values()}) == len(QUEUES)


def test_tasks_are_registered() -> None:
    assert {"ingestion.ingest_source", "ingestion.ingest_all", "embedding.embed_pending"} <= set(app.tasks)
