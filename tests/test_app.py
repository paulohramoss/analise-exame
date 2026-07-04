"""
Testes de integração dos fluxos de autenticação e checkout (app.py):
/login, /admin/entrar e /checkout/pay.

core.db fica com persistência desativada (SUPABASE_URL vazio no ambiente de
teste, ver conftest.py); onde o fluxo depende de um valor de retorno específico
(ex.: senha cadastrada, pagamento confirmado), a função é mockada explicitamente.
core.asaas nunca faz requisições HTTP reais nestes testes.
"""

from unittest.mock import MagicMock

from werkzeug.security import generate_password_hash

import app as flask_app_module
from core import asaas


# ── /login ────────────────────────────────────────────────────────────────────

def test_login_get_renders_form(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_login_post_missing_fields_shows_error(client):
    resp = client.post("/login", data={"email": "", "senha": ""})
    assert resp.status_code == 200
    assert "Preencha e-mail e senha" in resp.get_data(as_text=True)


def test_login_post_email_without_active_plan_shows_error(client, monkeypatch):
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_confirmado_por_email", lambda email: None)

    resp = client.post("/login", data={"email": "sem-plano@example.com", "senha": "qualquer123"})

    assert resp.status_code == 200
    assert "sem plano ativo" in resp.get_data(as_text=True)


def test_login_post_wrong_password_shows_error(client, monkeypatch):
    monkeypatch.setattr(
        flask_app_module.db, "buscar_pagamento_confirmado_por_email",
        lambda email: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(
        flask_app_module.db, "buscar_senha_hash_cliente",
        lambda email: generate_password_hash("senha-correta"),
    )

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "senha-errada"})

    assert resp.status_code == 200
    assert "Senha incorreta" in resp.get_data(as_text=True)


def test_login_post_correct_password_sets_premium_cookie(client, monkeypatch):
    monkeypatch.setattr(
        flask_app_module.db, "buscar_pagamento_confirmado_por_email",
        lambda email: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(
        flask_app_module.db, "buscar_senha_hash_cliente",
        lambda email: generate_password_hash("senha-correta"),
    )
    monkeypatch.setattr(flask_app_module.db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_sessao", MagicMock())

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "senha-correta"})

    assert resp.status_code == 302
    assert flask_app_module.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


def test_login_post_without_password_set_redirects_to_definir_senha(client, monkeypatch):
    monkeypatch.setattr(
        flask_app_module.db, "buscar_pagamento_confirmado_por_email",
        lambda email: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(flask_app_module.db, "buscar_senha_hash_cliente", lambda email: None)

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "qualquer123"})

    assert resp.status_code == 302
    assert "/login/definir-senha" in resp.headers["Location"]


def test_login_already_premium_redirects_home(client, monkeypatch):
    monkeypatch.setattr(flask_app_module, "is_premium", lambda req: True)
    resp = client.get("/login")
    assert resp.status_code == 302


# ── /admin/entrar ─────────────────────────────────────────────────────────────

def test_admin_entrar_without_admin_key_configured_returns_403(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "")
    monkeypatch.setattr(flask_app_module, "_is_local_runtime", lambda: False)

    resp = client.get("/admin/entrar?key=qualquer")

    assert resp.status_code == 403


def test_admin_entrar_with_wrong_key_returns_403(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "chave-correta")

    resp = client.get("/admin/entrar?key=chave-errada")

    assert resp.status_code == 403


def test_admin_entrar_with_correct_key_sets_admin_cookie(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "chave-correta")

    resp = client.get("/admin/entrar?key=chave-correta")

    assert resp.status_code == 302
    assert flask_app_module._ADMIN_COOKIE in resp.headers.get("Set-Cookie", "")


def test_admin_sair_removes_admin_cookie(client):
    resp = client.get("/admin/sair")
    assert resp.status_code == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert flask_app_module._ADMIN_COOKIE in set_cookie


# ── /checkout/pay ─────────────────────────────────────────────────────────────

def _valid_pix_form(**overrides):
    form = {
        "name": "Fulano de Tal",
        "email": "fulano@example.com",
        "cpf_cnpj": "12345678900",
        "plan": "anual",
        "billing_type": "PIX",
        "senha_acesso": "senha1234",
        "senha_confirmar": "senha1234",
    }
    form.update(overrides)
    return form


def test_checkout_pay_missing_name_or_email_returns_400(client):
    resp = client.post("/checkout/pay", data=_valid_pix_form(name=""))
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_checkout_pay_missing_cpf_returns_400(client):
    resp = client.post("/checkout/pay", data=_valid_pix_form(cpf_cnpj=""))
    assert resp.status_code == 400


def test_checkout_pay_short_password_returns_400(client):
    resp = client.post("/checkout/pay", data=_valid_pix_form(senha_acesso="123", senha_confirmar="123"))
    assert resp.status_code == 400
    assert "senha" in resp.get_json()["error"].lower()


def test_checkout_pay_password_mismatch_returns_400(client):
    resp = client.post("/checkout/pay", data=_valid_pix_form(senha_confirmar="outra-senha"))
    assert resp.status_code == 400
    assert "não coincidem" in resp.get_json()["error"]


def test_checkout_pay_credit_card_missing_card_number_returns_400(client):
    resp = client.post(
        "/checkout/pay",
        data=_valid_pix_form(billing_type="CREDIT_CARD", card_expiry="12/30", card_cvv="123",
                              card_postal_code="12345000", card_address_number="100"),
    )
    assert resp.status_code == 400
    assert "cartão" in resp.get_json()["error"].lower()


def test_checkout_pay_without_asaas_api_key_returns_503(client, monkeypatch):
    monkeypatch.setenv("ASAAS_API_KEY", "")
    resp = client.post("/checkout/pay", data=_valid_pix_form())
    assert resp.status_code == 503


def test_checkout_pay_asaas_failure_returns_502(client, monkeypatch):
    monkeypatch.setattr(asaas, "create_customer", MagicMock(side_effect=Exception("boom")))

    resp = client.post("/checkout/pay", data=_valid_pix_form())

    assert resp.status_code == 502
    assert resp.get_json()["success"] is False


def test_checkout_pay_pix_success_returns_qr_code(client, monkeypatch):
    monkeypatch.setattr(asaas, "create_customer", lambda name, email, cpf_cnpj: {"id": "cus_1"})
    monkeypatch.setattr(
        asaas, "create_payment",
        lambda **kwargs: {"id": "pay_1", "invoiceUrl": "https://asaas.example/pay_1"},
    )
    monkeypatch.setattr(
        asaas, "get_pix_qr_code", lambda payment_id: {"encodedImage": "img-b64", "payload": "00020126"}
    )
    monkeypatch.setattr(flask_app_module.db, "upsert_cliente", lambda *a, **k: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_senha_cliente", MagicMock())
    monkeypatch.setattr(flask_app_module.db, "salvar_pagamento", MagicMock())

    resp = client.post("/checkout/pay", data=_valid_pix_form())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["billing_type"] == "PIX"
    assert body["payment_id"] == "pay_1"
    assert body["pix_qr_image"] == "img-b64"


def test_checkout_pay_credit_card_confirmed_sets_premium_cookie(client, monkeypatch):
    monkeypatch.setattr(asaas, "create_customer", lambda name, email, cpf_cnpj: {"id": "cus_1"})
    monkeypatch.setattr(
        asaas, "create_payment",
        lambda **kwargs: {"id": "pay_1", "status": "CONFIRMED"},
    )
    monkeypatch.setattr(flask_app_module.db, "upsert_cliente", lambda *a, **k: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_senha_cliente", MagicMock())
    monkeypatch.setattr(flask_app_module.db, "salvar_pagamento", MagicMock())
    monkeypatch.setattr(flask_app_module.db, "buscar_senha_hash_cliente", lambda email: "hash-existente")
    monkeypatch.setattr(flask_app_module.db, "atualizar_status_pagamento", MagicMock())
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_sessao", MagicMock())

    resp = client.post(
        "/checkout/pay",
        data=_valid_pix_form(
            billing_type="CREDIT_CARD",
            card_number="4111111111111111",
            card_expiry="12/30",
            card_cvv="123",
            card_postal_code="12345000",
            card_address_number="100",
        ),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["confirmed"] is True
    assert flask_app_module.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")
