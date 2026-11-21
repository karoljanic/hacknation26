"""Request limits for ``POST /check``: abuse and cost protection.

Every check calls paid/quota-limited providers (LLM, decision model, source
APIs), so the endpoint admits a request only when all of these hold:

* per client: at most ``per_minute`` and ``per_day`` checks (sliding windows);
* globally: at most ``global_per_day`` checks, a hard ceiling on spend;
* concurrency: at most ``max_concurrent`` checks running at once.

Rejections are explicit HTTP 429/503 with ``Retry-After``, never a verdict.
State is in memory and per process (restart resets it); run one API process
or move the counters to a shared store before scaling out.

Client identity is the socket peer address. ``X-Forwarded-For`` is used only
when ``CHECK_TRUST_FORWARDED_FOR`` is set, because it is client-controlled
unless a reverse proxy in front of the API overwrites it; then its *last*
entry (the one that proxy appended) is used.
"""

import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

MINUTE, DAY = 60.0, 86_400.0
MAX_TRACKED_CLIENTS = 10_000


@dataclass(frozen=True)
class CheckLimits:
    per_minute: int = 20
    per_day: int = 300
    global_per_day: int = 5_000
    max_concurrent: int = 8

    def __post_init__(self) -> None:
        if min(self.per_minute, self.per_day, self.global_per_day, self.max_concurrent) < 1:
            raise ValueError("All request limits must be positive")

    @classmethod
    def from_env(cls) -> "CheckLimits":
        def read(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from None

        defaults = cls()
        return cls(
            per_minute=read("CHECK_RATE_LIMIT_PER_MINUTE", defaults.per_minute),
            per_day=read("CHECK_RATE_LIMIT_PER_DAY", defaults.per_day),
            global_per_day=read("CHECK_GLOBAL_LIMIT_PER_DAY", defaults.global_per_day),
            max_concurrent=read("CHECK_MAX_CONCURRENT", defaults.max_concurrent),
        )


class SlidingWindow:
    """Counts hits per key inside a trailing window. Not thread-safe by itself."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self.hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        queue = self.hits.setdefault(key, deque())
        while queue and queue[0] <= now - self.window:
            queue.popleft()
        return queue

    def retry_after(self, key: str, now: float) -> float | None:
        """Seconds until a hit is allowed again, or None when one is allowed now."""
        queue = self._prune(key, now)
        if len(queue) < self.limit:
            return None
        return queue[0] + self.window - now

    def record(self, key: str, now: float) -> None:
        self._prune(key, now).append(now)
        if len(self.hits) > MAX_TRACKED_CLIENTS:
            self._evict(now)

    def _evict(self, now: float) -> None:
        for key in list(self.hits):
            if not self._prune(key, now):
                del self.hits[key]


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float, *, global_limit: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = max(1, math.ceil(retry_after))
        self.global_limit = global_limit


class CheckGuard:
    def __init__(self, limits: CheckLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._minute = SlidingWindow(limits.per_minute, MINUTE)
        self._day = SlidingWindow(limits.per_day, DAY)
        self._global = SlidingWindow(limits.global_per_day, DAY)
        self._slots = threading.BoundedSemaphore(limits.max_concurrent)

    def admit(self, client: str) -> None:
        """Counts one check and takes a concurrency slot (release with ``release``).

        Nothing is counted when the request is rejected, so a rejected
        request neither consumes quota nor blocks others.
        """
        now = self._clock()
        with self._lock:
            wait = self._global.retry_after("*", now)
            if wait is not None:
                raise RateLimited(
                    "Usługa osiągnęła dzienny limit sprawdzeń. Spróbuj ponownie później.",
                    wait, global_limit=True,
                )
            for window, text in ((self._minute, "w ciągu minuty"), (self._day, "w ciągu doby")):
                wait = window.retry_after(client, now)
                if wait is not None:
                    raise RateLimited(
                        f"Zbyt wiele zgłoszeń {text}. Spróbuj ponownie za {math.ceil(wait)} s.", wait
                    )
            if not self._slots.acquire(blocking=False):
                raise RateLimited(
                    "Usługa jest teraz mocno obciążona. Spróbuj ponownie za chwilę.", 5, global_limit=True
                )
            for window, key in ((self._minute, client), (self._day, client), (self._global, "*")):
                window.record(key, now)

    def release(self) -> None:
        self._slots.release()


def client_key(request: Request) -> str:
    if os.environ.get("CHECK_TRUST_FORWARDED_FOR", "").strip().lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


@lru_cache(maxsize=1)
def get_check_guard() -> CheckGuard:
    return CheckGuard(CheckLimits.from_env())


def admit_check(
    request: Request, guard: Annotated[CheckGuard, Depends(get_check_guard)]
) -> Iterator[None]:
    """FastAPI dependency: admit the request or answer 429/503, and free the
    concurrency slot once the request has been handled."""
    try:
        guard.admit(client_key(request))
    except RateLimited as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE if exc.global_limit else status.HTTP_429_TOO_MANY_REQUESTS
            ),
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    try:
        yield
    finally:
        guard.release()
