"""
Testes do cliente Asaas (core/asaas.py).

Nenhuma chamada de rede real é feita: requests.get/post/put são sempre mockados.
"""

from unittest.mock import MagicMock

import pytest
import requests

from core import asaas


def _fake_response(status_code=200, json_data=None, ok=True, reason="OK", text=""):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok
    resp.reason = reason
    resp.text = text
    resp.json.return_value = json_data or {}
    return resp


def test_base_url_defaults_to_sandbox(monkeypatch):
    monkeypatch.delenv("ASAAS_ENVIRONMENT", raising=False)
    assert asaas._base_url() == asaas.ASAAS_SANDBOX_URL


def test_base_url_uses_production_when_configured(monkeypatch):
    monkeypatch.setenv("ASAAS_ENVIRONMENT", "production")
    assert asaas._base_url() == asaas.ASAAS_PROD_URL


def test_raise_for_status_noop_when_ok():
    asaas._raise_for_status(_fake_response(ok=True))


def test_raise_for_status_extracts_asaas_error_description():
    resp = _fake_response(
        status_code=401,
        ok=False,
        reason="Unauthorized",
        json_data={"errors": [{"description": "Chave de API inválida"}]},
    )
    with pytest.raises(requests.HTTPError, match="Chave de API inválida"):
        asaas._raise_for_status(resp)


def test_raise_for_status_falls_back_to_raw_text_when_body_not_json():
    resp = _fake_response(status_code=500, ok=False, reason="Internal Server Error", text="<html>erro</html>")
    resp.json.side_effect = ValueError("not json")
    with pytest.raises(requests.HTTPError, match="erro"):
        asaas._raise_for_status(resp)


def test_create_payment_invalid_billing_type_falls_back_to_undefined(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return _fake_response(json_data={"id": "pay_123"})

    monkeypatch.setattr(asaas.requests, "post", fake_post)

    result = asaas.create_payment(
        customer_id="cus_1", value=97.0, description="Plano", billing_type="ALGO_INVALIDO"
    )

    assert result == {"id": "pay_123"}
    assert captured["json"]["billingType"] == "UNDEFINED"


def test_create_payment_credit_card_attaches_card_payload(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return _fake_response(json_data={"id": "pay_456"})

    monkeypatch.setattr(asaas.requests, "post", fake_post)

    card = {"number": "4111", "holderName": "Fulano"}
    holder_info = {"email": "fulano@example.com"}
    asaas.create_payment(
        customer_id="cus_1",
        value=97.0,
        description="Plano",
        billing_type="CREDIT_CARD",
        credit_card=card,
        credit_card_holder_info=holder_info,
    )

    assert captured["json"]["creditCard"] == card
    assert captured["json"]["creditCardHolderInfo"] == holder_info


def test_is_payment_confirmed_true_for_confirmed_status(monkeypatch):
    monkeypatch.setattr(asaas, "get_payment", lambda payment_id: {"status": "CONFIRMED"})
    assert asaas.is_payment_confirmed("pay_1") is True


def test_is_payment_confirmed_false_for_pending_status(monkeypatch):
    monkeypatch.setattr(asaas, "get_payment", lambda payment_id: {"status": "PENDING"})
    assert asaas.is_payment_confirmed("pay_1") is False


def test_is_payment_confirmed_false_when_lookup_fails(monkeypatch):
    def boom(payment_id):
        raise requests.ConnectionError("timeout")

    monkeypatch.setattr(asaas, "get_payment", boom)
    assert asaas.is_payment_confirmed("pay_1") is False
