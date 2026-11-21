from decimal import Decimal

from prawdowid.services.usage import MeteredDecision, UsageMeter, estimate_cost, model_prices, usage_entry


def test_cost_is_summed_over_priced_calls() -> None:
    prices = {"m": (Decimal(1), Decimal(10))}
    models = [usage_entry("a", "m", None, 1_000_000, 100_000), {"role": "embedding", "model": "m", "calls": 2}]

    assert estimate_cost(models, prices) == Decimal("2.000000")


def test_cost_is_unknown_rather_than_understated_when_a_model_has_no_price() -> None:
    models = [usage_entry("a", "m", None, 10, 1), usage_entry("b", "unpriced", None, 10, 1)]

    assert estimate_cost(models, {"m": (Decimal(1), Decimal(1))}) is None
    assert estimate_cost([], {}) is None


def test_prices_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_PRICES_USD_PER_MTOK", '{"m": [0.3, 2.5]}')
    assert model_prices() == {"m": (Decimal("0.3"), Decimal("2.5"))}
    monkeypatch.setenv("MODEL_PRICES_USD_PER_MTOK", "not json")
    assert model_prices() == {}


def test_metered_decision_records_usage() -> None:
    from unittest.mock import Mock

    inner = Mock()
    inner.decide.return_value = Mock(model="jev", input_tokens=7, output_tokens=2)
    meter = UsageMeter()

    MeteredDecision(inner, meter).decide("state", {})

    assert meter.entries() == [usage_entry("decision", "jev", None, 7, 2)]
