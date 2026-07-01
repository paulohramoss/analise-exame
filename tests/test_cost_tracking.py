"""Testes do módulo de custo/orçamento (core/cost_tracking.py)."""

from core import cost_tracking


def test_estimate_cost_usd_known_model():
    cost = cost_tracking.estimate_cost_usd("gemini-2.5-flash", input_tokens=1_000_000, output_tokens=1_000_000)
    input_price, output_price = cost_tracking._PRICING_PER_1M_TOKENS["gemini-2.5-flash"]
    assert cost == input_price + output_price


def test_estimate_cost_usd_unknown_model_is_zero():
    assert cost_tracking.estimate_cost_usd("modelo-desconhecido", 1_000_000, 1_000_000) == 0.0


def test_log_analysis_cost_sums_all_usage_entries():
    usage = [
        {"model": "gemini-2.5-flash", "input_tokens": 1000, "output_tokens": 500},
        {"model": "claude-sonnet-4-6", "input_tokens": 800, "output_tokens": 400},
    ]
    expected = sum(
        cost_tracking.estimate_cost_usd(item["model"], item["input_tokens"], item["output_tokens"])
        for item in usage
    )
    total = cost_tracking.log_analysis_cost("joelho", "gemini-2.5-flash + claude-sonnet-4-6", usage)
    assert total == expected


def test_log_analysis_cost_empty_usage_is_zero():
    assert cost_tracking.log_analysis_cost("geral", "gemini-2.5-flash", []) == 0.0


def test_add_and_get_spend_accumulates_in_memory():
    scope = "test-scope-accumulate"
    assert cost_tracking.get_spend(scope) == 0.0
    cost_tracking.add_spend(scope, 1.5)
    cost_tracking.add_spend(scope, 2.5)
    assert cost_tracking.get_spend(scope) == 4.0


def test_is_budget_exceeded_false_when_cap_disabled():
    scope = "test-scope-disabled-cap"
    cost_tracking.add_spend(scope, 1000.0)
    assert cost_tracking.is_budget_exceeded(scope, cap_usd=0) is False


def test_is_budget_exceeded_true_once_cap_reached():
    scope = "test-scope-cap"
    cost_tracking.add_spend(scope, 3.0)
    assert cost_tracking.is_budget_exceeded(scope, cap_usd=3.0) is True
    assert cost_tracking.is_budget_exceeded(scope, cap_usd=5.0) is False


def test_add_spend_ignores_non_positive_amounts():
    scope = "test-scope-negative"
    cost_tracking.add_spend(scope, 5.0)
    cost_tracking.add_spend(scope, -100.0)
    assert cost_tracking.get_spend(scope) == 5.0
