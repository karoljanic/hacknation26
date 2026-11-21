import threading

import pytest
from fastapi.testclient import TestClient

from prawdowid.api.app import app
from prawdowid.api.rate_limit import CheckGuard, CheckLimits, RateLimited, get_check_guard


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def guard(clock: Clock | None = None, **limits: int) -> CheckGuard:
    return CheckGuard(CheckLimits(**{"per_minute": 2, "per_day": 100, "global_per_day": 100, "max_concurrent": 10, **limits}),
                      clock=clock or Clock())


def test_per_client_minute_limit_blocks_then_recovers() -> None:
    clock = Clock()
    g = guard(clock)
    for _ in range(2):
        g.admit("a")
        g.release()

    with pytest.raises(RateLimited) as blocked:
        g.admit("a")
    assert not blocked.value.global_limit and 1 <= blocked.value.retry_after <= 60

    g.admit("b")  # another client is unaffected
    g.release()
    clock.now += 61
    g.admit("a")  # the window has slid past the old hits
    g.release()


def test_rejected_request_does_not_consume_quota_or_a_slot() -> None:
    g = guard(per_minute=1, max_concurrent=1)
    g.admit("a")
    for _ in range(5):
        with pytest.raises(RateLimited):
            g.admit("a")
    g.release()
    assert len(g._global.hits["*"]) == 1  # only the admitted request counted


def test_per_day_limit_applies_independently_of_the_minute_limit() -> None:
    clock = Clock()
    g = guard(clock, per_minute=10, per_day=3)
    for _ in range(3):
        g.admit("a")
        g.release()
        clock.now += 120
    with pytest.raises(RateLimited, match="doby"):
        g.admit("a")


def test_global_daily_limit_is_a_hard_ceiling_across_clients() -> None:
    g = guard(global_per_day=2)
    g.admit("a")
    g.release()
    g.admit("b")
    g.release()
    with pytest.raises(RateLimited) as blocked:
        g.admit("c")
    assert blocked.value.global_limit


def test_concurrency_cap_rejects_instead_of_queueing_and_slot_is_reusable() -> None:
    g = guard(max_concurrent=1, per_minute=100)
    g.admit("a")
    with pytest.raises(RateLimited, match="obciążona"):
        g.admit("b")
    g.release()
    g.admit("b")
    g.release()


def test_guard_is_thread_safe_under_contention() -> None:
    g = guard(per_minute=50, max_concurrent=1000)
    admitted: list[int] = []

    def worker() -> None:
        try:
            g.admit("same")
            admitted.append(1)
        except RateLimited:
            pass

    threads = [threading.Thread(target=worker) for _ in range(200)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(admitted) == 50


def test_limits_reject_non_positive_values_and_bad_environment(monkeypatch) -> None:
    with pytest.raises(ValueError):
        CheckLimits(per_minute=0)
    monkeypatch.setenv("CHECK_RATE_LIMIT_PER_MINUTE", "many")
    with pytest.raises(ValueError, match="CHECK_RATE_LIMIT_PER_MINUTE"):
        CheckLimits.from_env()
    monkeypatch.setenv("CHECK_RATE_LIMIT_PER_MINUTE", "7")
    assert CheckLimits.from_env().per_minute == 7


# -- through the HTTP endpoint ------------------------------------------------


@pytest.fixture
def api():
    yield TestClient(app)
    app.dependency_overrides.pop(get_check_guard, None)


def test_endpoint_answers_429_with_retry_after_and_never_a_verdict(api) -> None:
    g = guard(per_minute=1)
    app.dependency_overrides[get_check_guard] = lambda: g
    form = {"type": "text", "text": "Treść."}

    api.post("/check", data=form)  # admitted (outcome irrelevant here)
    blocked = api.post("/check", data=form)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert set(blocked.json()) == {"detail"}


def test_endpoint_answers_503_when_the_global_ceiling_is_reached(api) -> None:
    g = guard(global_per_day=1)
    app.dependency_overrides[get_check_guard] = lambda: g
    form = {"type": "text", "text": "Treść."}

    api.post("/check", data=form)
    blocked = api.post("/check", data=form)

    assert blocked.status_code == 503
    assert "Retry-After" in blocked.headers


def test_slot_is_released_after_each_request(api) -> None:
    g = guard(per_minute=100, max_concurrent=1)
    app.dependency_overrides[get_check_guard] = lambda: g

    statuses = [api.post("/check", data={"type": "text", "text": "Treść."}).status_code for _ in range(4)]

    assert 503 not in statuses and 429 not in statuses


def test_health_endpoints_are_not_limited(api) -> None:
    g = guard(per_minute=1, global_per_day=1)
    app.dependency_overrides[get_check_guard] = lambda: g
    assert [api.get("/health").status_code for _ in range(5)] == [200] * 5
