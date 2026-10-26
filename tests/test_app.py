"""
Testes de integração dos fluxos de autenticação e checkout (app.py):
/login, /admin/entrar e /checkout/pay.

core.db fica com persistência desativada (SUPABASE_URL vazio no ambiente de
teste, ver conftest.py); onde o fluxo depende de um valor de retorno específico
(ex.: senha cadastrada, pagamento confirmado), a função é mockada explicitamente.
core.asaas nunca faz requisições HTTP reais nestes testes.
"""

from unittest.mock import MagicMock

import pytest
from werkzeug.security import generate_password_hash

from web.routes import billing as billing_routes
from web import config
from core import db
from web import security
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
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)

    resp = client.post("/login", data={"email": "sem-plano@example.com", "senha": "qualquer123"})

    assert resp.status_code == 200
    assert "E-mail ou senha inválidos" in resp.get_data(as_text=True)


def test_login_post_wrong_password_shows_error(client, monkeypatch):
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(
        db, "buscar_senha_hash_cliente",
        lambda email: generate_password_hash("senha-correta"),
    )

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "senha-errada"})

    assert resp.status_code == 200
    assert "E-mail ou senha inválidos" in resp.get_data(as_text=True)


def test_login_post_correct_password_sets_premium_cookie(client, monkeypatch):
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(
        db, "buscar_senha_hash_cliente",
        lambda email: generate_password_hash("senha-correta"),
    )
    monkeypatch.setattr(db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(db, "salvar_sessao", MagicMock())

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "senha-correta"})

    assert resp.status_code == 302
    assert security.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


def test_login_post_without_password_set_does_not_hand_out_setup_link(client, monkeypatch):
    """Conta sem senha não pode ganhar link de definição só por quem digita o e-mail."""
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: None)

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "qualquer123"})

    assert resp.status_code == 200
    assert "definir-senha" not in resp.headers.get("Location", "")
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")


def test_login_post_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)
    statuses = [
        client.post("/login", data={"email": "alvo@example.com", "senha": f"tentativa{i}"}).status_code
        for i in range(11)
    ]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_login_already_premium_redirects_home(client, monkeypatch):
    monkeypatch.setattr(security, "is_premium", lambda req: True)
    resp = client.get("/login")
    assert resp.status_code == 302


# ── /admin/entrar ─────────────────────────────────────────────────────────────

def test_admin_entrar_without_admin_key_configured_returns_403(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "")
    monkeypatch.setattr(config, "is_local_runtime", lambda: False)

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
    assert security.ADMIN_COOKIE in resp.headers.get("Set-Cookie", "")


def test_admin_sair_removes_admin_cookie(client):
    resp = client.get("/admin/sair")
    assert resp.status_code == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert security.ADMIN_COOKIE in set_cookie


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


@pytest.mark.parametrize("email", [
    "",
    "sem-arroba",
    "fulano@dominio",
    "fulano..silva@clinica.com.br",
    "fulano@mailinator.com",
])
def test_checkout_pay_invalid_email_returns_400(client, email):
    resp = client.post("/checkout/pay", data=_valid_pix_form(email=email))
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_checkout_pay_rejects_domain_without_mx(client, monkeypatch):
    monkeypatch.setattr(billing_routes, "validar_email_cadastro",
                        lambda e: ("", "O domínio deste e-mail não existe ou não recebe mensagens."))
    resp = client.post("/checkout/pay", data=_valid_pix_form(email="arroz@arroz.com"))
    assert resp.status_code == 400
    assert "domínio" in resp.get_json()["error"]


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
    monkeypatch.setattr(db, "upsert_cliente", lambda *a, **k: "cliente-1")
    monkeypatch.setattr(db, "salvar_senha_cliente", MagicMock())
    monkeypatch.setattr(db, "salvar_pagamento", MagicMock())

    resp = client.post("/checkout/pay", data=_valid_pix_form())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["billing_type"] == "PIX"
    assert body["payment_id"] == "pay_1"
    assert body["pix_qr_image"] == "img-b64"
    # Marca este navegador como dono da cobrança — só ele recebe o acesso no polling.
    assert security.owns_checkout(_request_with_cookies(resp), "pay_1")
    assert not security.owns_checkout(_request_with_cookies(resp), "pay_outro")


def _request_with_cookies(resp):
    from http.cookies import SimpleCookie

    jar = SimpleCookie()
    for header in resp.headers.getlist("Set-Cookie"):
        jar.load(header)

    class _Req:
        cookies = {key: morsel.value for key, morsel in jar.items()}

    return _Req()


def _mock_asaas_checkout(monkeypatch, create_payment):
    monkeypatch.setattr(asaas, "create_customer", lambda name, email, cpf_cnpj: {"id": "cus_1"})
    monkeypatch.setattr(asaas, "create_payment", create_payment)
    monkeypatch.setattr(db, "upsert_cliente", lambda *a, **k: "cliente-1")
    monkeypatch.setattr(db, "salvar_senha_cliente", MagicMock())
    monkeypatch.setattr(db, "salvar_pagamento", MagicMock())


def test_checkout_pay_credit_card_returns_asaas_invoice_without_card_data(client, monkeypatch):
    create_payment = MagicMock(return_value={"id": "pay_1", "invoiceUrl": "https://asaas.example/i/pay_1"})
    _mock_asaas_checkout(monkeypatch, create_payment)

    # Campos de cartão enviados por um formulário antigo são simplesmente ignorados.
    resp = client.post(
        "/checkout/pay",
        data=_valid_pix_form(billing_type="CREDIT_CARD", card_number="4111111111111111", card_cvv="123"),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {
        "success": True,
        "billing_type": "CREDIT_CARD",
        "payment_id": "pay_1",
        "invoice_url": "https://asaas.example/i/pay_1",
    }
    kwargs = create_payment.call_args.kwargs
    assert kwargs["billing_type"] == "CREDIT_CARD"
    assert not any("card" in key for key in kwargs)
    # Acesso só depois da confirmação, pelo polling — nunca na criação da cobrança.
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")


def test_checkout_pay_credit_card_passes_chosen_installments(client, monkeypatch):
    create_payment = MagicMock(return_value={"id": "pay_1", "invoiceUrl": "https://asaas.example/i/pay_1"})
    _mock_asaas_checkout(monkeypatch, create_payment)

    resp = client.post("/checkout/pay", data=_valid_pix_form(billing_type="CREDIT_CARD", installments="10"))

    assert resp.status_code == 200
    kwargs = create_payment.call_args.kwargs
    assert kwargs["installment_count"] == 10
    assert kwargs["value"] == 418.00


@pytest.mark.parametrize(
    "form, expected",
    [
        ({"billing_type": "CREDIT_CARD", "installments": "99"}, 12),  # acima do limite do plano
        ({"billing_type": "CREDIT_CARD", "installments": "abc"}, 1),
        ({"billing_type": "CREDIT_CARD", "installments": "6", "plan": "mensal"}, 1),  # plano sem parcelamento
        ({"billing_type": "PIX", "installments": "12"}, 1),  # PIX/boleto sempre à vista
        ({"billing_type": "BOLETO", "installments": "12"}, 1),
    ],
)
def test_checkout_pay_clamps_installments(client, monkeypatch, form, expected):
    create_payment = MagicMock(return_value={"id": "pay_1", "invoiceUrl": "https://asaas.example/i/pay_1"})
    _mock_asaas_checkout(monkeypatch, create_payment)
    monkeypatch.setattr(asaas, "get_pix_qr_code", lambda pid: {})
    monkeypatch.setattr(asaas, "get_boleto_identification", lambda pid: {})

    resp = client.post("/checkout/pay", data=_valid_pix_form(**form))

    assert resp.status_code == 200
    assert create_payment.call_args.kwargs["installment_count"] == expected


def test_checkout_pay_asaas_error_does_not_leak_details(client, monkeypatch):
    def _fail(**kwargs):
        raise RuntimeError("401 Unauthorized — access_token=$aact_SEGREDO")

    _mock_asaas_checkout(monkeypatch, _fail)

    resp = client.post("/checkout/pay", data=_valid_pix_form())

    assert resp.status_code == 502
    assert "SEGREDO" not in resp.get_data(as_text=True)
    assert "401" not in resp.get_json()["error"]
