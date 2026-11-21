"""Per-request model usage: which models were called, with how many tokens,
and (when prices are configured) what it cost. Stored on ``verification_run``
so cost and latency can be reported per check, as the task requires.
"""

import json
import logging
import os
import threading
from decimal import Decimal
from typing import Any

from prawdowid.ai.adapters.decision import DecisionAdapter, DecisionQuestion, DecisionResult
from prawdowid.ai.adapters.media import Media

log = logging.getLogger(__name__)

# {"<model>": [usd_per_1M_input_tokens, usd_per_1M_output_tokens], ...}
PRICES_ENV = "MODEL_PRICES_USD_PER_MTOK"


def usage_entry(
    role: str, model: str, version: str | None, input_tokens: int | None, output_tokens: int | None
) -> dict[str, Any]:
    return {
        "role": role,
        "model": model,
        "model_version": version,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


class UsageMeter:
    """Collects usage from calls made in worker threads (thread-safe)."""

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(entry)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)


class MeteredDecision:
    """A ``DecisionAdapter`` that records the usage of every call."""

    def __init__(self, inner: DecisionAdapter, meter: UsageMeter, *, role: str = "decision") -> None:
        self.inner = inner
        self.meter = meter
        self.role = role

    def decide(
        self, state: str, questions: dict[str, DecisionQuestion], *, image: Media | None = None
    ) -> DecisionResult:
        result = self.inner.decide(state, questions, image=image)
        self.meter.record(usage_entry(self.role, result.model, None, result.input_tokens, result.output_tokens))
        return result


def model_prices() -> dict[str, tuple[Decimal, Decimal]]:
    raw = os.environ.get(PRICES_ENV, "").strip()
    if not raw:
        return {}
    try:
        return {
            model: (Decimal(str(prices[0])), Decimal(str(prices[1])))
            for model, prices in json.loads(raw).items()
        }
    except (ValueError, TypeError, IndexError, AttributeError):
        log.warning("%s is not valid JSON {model: [input, output]}; cost is not estimated", PRICES_ENV)
        return {}


def estimate_cost(models: list[dict[str, Any]], prices: dict[str, tuple[Decimal, Decimal]]) -> Decimal | None:
    """Sum over priced calls; ``None`` when any call with tokens has no price
    (a partial sum would understate the cost)."""
    total = Decimal(0)
    priced_any = False
    for entry in models:
        input_tokens, output_tokens = entry.get("input_tokens"), entry.get("output_tokens")
        if input_tokens is None and output_tokens is None:
            continue
        price = prices.get(entry.get("model") or "")
        if price is None:
            return None
        total += (Decimal(input_tokens or 0) * price[0] + Decimal(output_tokens or 0) * price[1]) / 1_000_000
        priced_any = True
    return total.quantize(Decimal("0.000001")) if priced_any else None
