"""
Testes de integração dos endpoints de pagamento (/webhook/asaas, /api/payment/status).

core.db fica com persistência desativada (SUPABASE_URL vazio no ambiente de teste,
ver conftest.py), então essas chamadas já são no-ops seguros. core.asaas é sempre
mockado para nunca fazer requisições HTTP reais.
"""

from unittest.mock import MagicMock

from core import db
from web import security
from core import asaas


def test_webhook_accepts_request_when_no_token_configured(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "")
    salvar_webhook = MagicMock()
    atualizar_status = MagicMock()
    monkeypatch.setattr(db, "salvar_webhook", salvar_webhook)
    monkeypatch.setattr(db, "atualizar_status_pagamento", atualizar_status)

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
    monkeypatch.setattr(db, "salvar_webhook", MagicMock())
    monkeypatch.setattr(db, "atualizar_status_pagamento", MagicMock())

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


def _mark_as_checkout_owner(client, payment_id="pay_1"):
    token = security.serializer().dumps({"pid": payment_id}, salt=security.CHECKOUT_SALT)
    client.set_cookie(security.CHECKOUT_COOKIE, token)


def test_payment_status_confirmed_without_checkout_cookie_requires_login(client, monkeypatch):
    """Conhecer o payment_id (URL, histórico) não pode liberar o acesso de outra pessoa."""
    monkeypatch.setattr(asaas, "is_payment_confirmed", lambda payment_id: True)
    get_payment = MagicMock(return_value={"externalReference": "cliente@example.com|mensal"})
    monkeypatch.setattr(asaas, "get_payment", get_payment)
    monkeypatch.setattr(db, "atualizar_status_pagamento", MagicMock())

    resp = client.get("/api/payment/status?id=pay_1")

    assert resp.status_code == 200
    assert resp.get_json() == {"confirmed": True, "login_required": True}
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")
    get_payment.assert_not_called()


def test_payment_status_cookie_for_other_payment_requires_login(client, monkeypatch):
    monkeypatch.setattr(asaas, "is_payment_confirmed", lambda payment_id: True)
    monkeypatch.setattr(db, "atualizar_status_pagamento", MagicMock())
    _mark_as_checkout_owner(client, "pay_outro")

    resp = client.get("/api/payment/status?id=pay_1")

    assert resp.get_json()["login_required"] is True
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")


def test_payment_status_confirmed_sets_premium_cookie(client, monkeypatch):
    _mark_as_checkout_owner(client)
    monkeypatch.setattr(asaas, "is_payment_confirmed", lambda payment_id: True)
    monkeypatch.setattr(
        asaas, "get_payment", lambda payment_id: {"externalReference": "cliente@example.com|mensal"}
    )
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: "hash-existente")
    monkeypatch.setattr(db, "atualizar_status_pagamento", MagicMock())
    monkeypatch.setattr(db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(db, "salvar_sessao", MagicMock())

    resp = client.get("/api/payment/status?id=pay_1")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["confirmed"] is True
    assert security.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


def test_payment_status_missing_id_returns_400(client):
    resp = client.get("/api/payment/status")
    assert resp.status_code == 400
