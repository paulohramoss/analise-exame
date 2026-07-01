"""
Testes do orçamento de gasto diário integrado às rotas de análise (app.py):
- bloqueio quando o orçamento do trial já foi excedido
- separação de custo próprio vs. custo do chamador no modo BYOK da API
"""

from unittest.mock import MagicMock

import app as flask_app_module
from core import cost_tracking


def test_trial_analyze_blocked_when_daily_budget_exceeded(client, monkeypatch):
    monkeypatch.setenv("TRIAL_DAILY_COST_BUDGET_USD", "1.00")
    monkeypatch.setattr(cost_tracking, "is_budget_exceeded", lambda scope, cap: scope == "trial")

    resp = client.post(
        "/trial/analyze", data={"description": "dor no joelho", "privacy_consent": "accepted"}
    )

    assert resp.status_code == 429
    assert "limite" in resp.get_json()["error"].lower()


def test_trial_analyze_not_blocked_when_budget_disabled(monkeypatch):
    monkeypatch.setenv("TRIAL_DAILY_COST_BUDGET_USD", "0")
    monkeypatch.setenv("GLOBAL_DAILY_COST_BUDGET_USD", "0")
    assert flask_app_module._blocked_by_cost_budget("trial") is False


def test_cost_budget_scopes_trial_includes_trial_scope(monkeypatch):
    monkeypatch.setenv("TRIAL_DAILY_COST_BUDGET_USD", "2.50")
    monkeypatch.setenv("GLOBAL_DAILY_COST_BUDGET_USD", "0")
    monkeypatch.setenv("USER_DAILY_COST_BUDGET_USD", "0")
    scopes = flask_app_module._cost_budget_scopes("trial")
    assert scopes == [("trial", 2.5)]


def test_cost_budget_scopes_global_applies_to_all_modes(monkeypatch):
    monkeypatch.setenv("GLOBAL_DAILY_COST_BUDGET_USD", "10.00")
    monkeypatch.setenv("TRIAL_DAILY_COST_BUDGET_USD", "0")
    monkeypatch.setenv("USER_DAILY_COST_BUDGET_USD", "0")
    with flask_app_module.app.test_request_context("/analyze"):
        assert flask_app_module._cost_budget_scopes("premium") == [("global", 10.0)]


def test_budget_user_key_premium_uses_session_email():
    with flask_app_module.app.test_request_context("/analyze"):
        flask_app_module.g.user_email = "paciente@example.com"
        assert flask_app_module._budget_user_key("premium") == "paciente@example.com"


def test_budget_user_key_api_hashes_supplied_key():
    with flask_app_module.app.test_request_context(
        "/api/analyze", headers={"X-API-Key": "minha-chave-secreta"}
    ):
        key = flask_app_module._budget_user_key("api")
        assert key is not None
        assert key.startswith("apikey:")
        assert "minha-chave-secreta" not in key  # nunca a chave em texto puro


def test_budget_user_key_api_without_supplied_key_is_none():
    with flask_app_module.app.test_request_context("/api/analyze"):
        assert flask_app_module._budget_user_key("api") is None


def test_own_cost_usage_excludes_gemini_when_caller_supplies_key():
    usage = [
        {"model": "gemini-2.5-flash", "input_tokens": 1000, "output_tokens": 500},
        {"model": "claude-sonnet-4-6", "input_tokens": 800, "output_tokens": 400},
    ]
    with flask_app_module.app.test_request_context(
        "/api/analyze", headers={"X-API-Key": "chave-do-chamador"}
    ):
        own_usage = flask_app_module._own_cost_usage("api", usage)

    assert own_usage == [usage[1]]  # só a parcela do Claude, que ainda é nossa


def test_own_cost_usage_keeps_everything_without_byok():
    usage = [
        {"model": "gemini-2.5-flash", "input_tokens": 1000, "output_tokens": 500},
        {"model": "claude-sonnet-4-6", "input_tokens": 800, "output_tokens": 400},
    ]
    with flask_app_module.app.test_request_context("/api/analyze"):
        own_usage = flask_app_module._own_cost_usage("api", usage)

    assert own_usage == usage


def test_own_cost_usage_keeps_everything_for_trial_and_premium():
    usage = [{"model": "gemini-2.5-flash", "input_tokens": 100, "output_tokens": 50}]
    with flask_app_module.app.test_request_context("/trial/analyze"):
        assert flask_app_module._own_cost_usage("trial", usage) == usage
    with flask_app_module.app.test_request_context("/analyze"):
        assert flask_app_module._own_cost_usage("premium", usage) == usage


def test_record_analysis_cost_adds_spend_to_active_scopes(monkeypatch):
    monkeypatch.setenv("TRIAL_DAILY_COST_BUDGET_USD", "5.00")
    monkeypatch.setenv("GLOBAL_DAILY_COST_BUDGET_USD", "0")
    monkeypatch.setenv("USER_DAILY_COST_BUDGET_USD", "0")

    result = {
        "exam_type": "joelho",
        "model_used": "gemini-2.5-flash",
        "usage": [{"model": "gemini-2.5-flash", "input_tokens": 1_000_000, "output_tokens": 1_000_000}],
    }
    with flask_app_module.app.test_request_context("/trial/analyze"):
        flask_app_module._record_analysis_cost("trial", result)

    expected_cost = cost_tracking.estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost_tracking.get_spend("trial") == expected_cost


def test_record_analysis_cost_skips_zero_cost_cache_hit(monkeypatch):
    monkeypatch.setenv("TRIAL_DAILY_COST_BUDGET_USD", "5.00")
    add_spend_mock = MagicMock(wraps=cost_tracking.add_spend)
    monkeypatch.setattr(cost_tracking, "add_spend", add_spend_mock)

    result = {"exam_type": "joelho", "model_used": "gemini-2.5-flash", "usage": []}
    with flask_app_module.app.test_request_context("/trial/analyze"):
        flask_app_module._record_analysis_cost("trial", result)

    add_spend_mock.assert_not_called()
