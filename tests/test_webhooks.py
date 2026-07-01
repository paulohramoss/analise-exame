"""
Testes de integração dos endpoints de pagamento (/webhook/asaas, /api/payment/status).

core.db fica com persistência desativada (SUPABASE_URL vazio no ambiente de teste,
ver conftest.py), então essas chamadas já são no-ops seguros. core.asaas é sempre
mockado para nunca fazer requisições HTTP reais.
"""

from unittest.mock import MagicMock

import app as flask_app_module
from core import asaas


def test_webhook_accepts_request_when_no_token_configured(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "")
    salvar_webhook = MagicMock()
    atualizar_status = MagicMock()
    monkeypatch.setattr(flask_app_module.db, "salvar_webhook", salvar_webhook)
    monkeypatch.setattr(flask_app_module.db, "atualizar_status_pagamento", atualizar_status)

    payload = {
        "event": "PAYMENT_CONFIRMED",
        "payment": {"id": "pay_1", "status": "CONFIRMED"},
    }
    resp = client.post("/webhook/asaas", json=payload)

    assert resp.status_code == 200
    assert resp.get_json() == {"received": True}
    salvar_webhook.assert_called_once_with(
        evento="PAYMENT_CONFIRMED",
        asaas_payment_id="pay_1",
        status_pagamento="CONFIRMED",
        payload=payload,
    )
    atualizar_status.assert_called_once_with("pay_1", "CONFIRMED", payload=payload)


def test_webhook_rejects_request_with_wrong_token(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo-correto")

    resp = client.post(
        "/webhook/asaas",
        json={"event": "PAYMENT_CONFIRMED", "payment": {"id": "pay_1", "status": "CONFIRMED"}},
        headers={"asaas-access-token": "token-errado"},
    )

    assert resp.status_code == 401


def test_webhook_accepts_request_with_correct_token(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo-correto")
    monkeypatch.setattr(flask_app_module.db, "salvar_webhook", MagicMock())
    monkeypatch.setattr(flask_app_module.db, "atualizar_status_pagamento", MagicMock())

    resp = client.post(
        "/webhook/asaas",
        json={"event": "PAYMENT_CONFIRMED", "payment": {"id": "pay_1", "status": "CONFIRMED"}},
        headers={"asaas-access-token": "segredo-correto"},
    )

    assert resp.status_code == 200


def test_payment_status_returns_not_confirmed(client, monkeypatch):
    monkeypatch.setattr(asaas, "is_payment_confirmed", lambda payment_id: False)

    resp = client.get("/api/payment/status?id=pay_1")

    assert resp.status_code == 200
    assert resp.get_json() == {"confirmed": False}


def test_payment_status_confirmed_sets_premium_cookie(client, monkeypatch):
    monkeypatch.setattr(asaas, "is_payment_confirmed", lambda payment_id: True)
    monkeypatch.setattr(
        asaas, "get_payment", lambda payment_id: {"externalReference": "cliente@example.com|mensal"}
    )
    monkeypatch.setattr(flask_app_module.db, "buscar_senha_hash_cliente", lambda email: "hash-existente")
    monkeypatch.setattr(flask_app_module.db, "atualizar_status_pagamento", MagicMock())
    monkeypatch.setattr(flask_app_module.db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_sessao", MagicMock())

    resp = client.get("/api/payment/status?id=pay_1")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["confirmed"] is True
    assert flask_app_module.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


def test_payment_status_missing_id_returns_400(client):
    resp = client.get("/api/payment/status")
    assert resp.status_code == 400
