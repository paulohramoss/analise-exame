"""
Funil de vendas medido no servidor: visitante anônimo (cookie com UUID),
eventos das páginas de venda, motivo das falhas no checkout e a ligação
visitante → cliente no momento da compra. Nenhum dado digitado vai ao PostHog.
"""

from unittest.mock import MagicMock

import pytest

from core import asaas, db
from web import posthog

BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0"}


@pytest.fixture()
def fake_posthog(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(posthog, "posthog_client", client)
    return client


def _events(client):
    return [(c.args[0], c.kwargs.get("properties", {})) for c in client.capture.call_args_list]


def test_visitante_recebe_cookie_uuid_e_mantem_o_mesmo(client, fake_posthog):
    resp = client.get("/planos", headers=BROWSER)
    cookie = client.get_cookie(posthog.VISITOR_COOKIE)
    assert cookie is not None and posthog.visitor_id_from_cookie(cookie.value)
    assert "HttpOnly" in resp.headers.get("Set-Cookie", "")

    resp = client.get("/planos", headers=BROWSER)
    assert posthog.VISITOR_COOKIE not in resp.headers.get("Set-Cookie", "")


def test_cookie_adulterado_e_substituido(client, fake_posthog):
    client.set_cookie(posthog.VISITOR_COOKIE, "<script>")
    client.get("/planos", headers=BROWSER)
    assert posthog.visitor_id_from_cookie(client.get_cookie(posthog.VISITOR_COOKIE).value)


@pytest.mark.parametrize("path,event,props", [
    ("/planos?src=lp", "pricing_page_viewed", {"source": "lp"}),
    ("/trial", "trial_page_viewed", {"source": "direto"}),
    ("/checkout?plan=anual&src=trial_limite", "checkout_viewed", {"plan_key": "anual", "source": "trial_limite"}),
])
def test_paginas_do_funil_registram_evento(client, fake_posthog, path, event, props):
    client.get(path, headers=BROWSER)
    assert (event, props) in _events(fake_posthog)


def test_origem_e_limpa(client, fake_posthog):
    client.get("/planos?src=<b>Insta%20Gram</b>", headers=BROWSER)
    assert ("pricing_page_viewed", {"source": "binstagramb"}) in _events(fake_posthog)


def test_robo_nao_conta(client, fake_posthog):
    client.get("/planos", headers={"User-Agent": "Googlebot/2.1"})
    assert _events(fake_posthog) == []


def test_falha_no_checkout_registra_so_o_motivo(client, fake_posthog):
    client.post("/checkout/pay", headers=BROWSER, data={
        "name": "Fulano", "email": "fulano@example.com", "cpf_cnpj": "", "plan": "anual",
        "billing_type": "CREDIT_CARD", "senha_acesso": "senha1234", "senha_confirmar": "senha1234",
    })
    [(event, props)] = [e for e in _events(fake_posthog) if e[0] == "checkout_failed"]
    assert props == {"reason": "cpf_cnpj", "plan_key": "anual", "billing_type": "CREDIT_CARD"}
    assert "Fulano" not in str(fake_posthog.mock_calls) and "fulano@" not in str(fake_posthog.mock_calls)


def test_compra_liga_visitante_ao_cliente(client, fake_posthog, monkeypatch):
    monkeypatch.setattr(asaas, "create_customer", lambda name, email, cpf_cnpj: {"id": "cus_1"})
    monkeypatch.setattr(asaas, "create_payment", MagicMock(return_value={"id": "pay_1", "invoiceUrl": "https://x"}))
    monkeypatch.setattr(db, "upsert_cliente", lambda *a, **k: "cliente-1")
    monkeypatch.setattr(db, "salvar_senha_cliente", MagicMock())
    monkeypatch.setattr(db, "salvar_pagamento", MagicMock())
    monkeypatch.setenv("ASAAS_API_KEY", "teste")

    client.get("/trial", headers=BROWSER)
    visitor = client.get_cookie(posthog.VISITOR_COOKIE).value
    resp = client.post("/checkout/pay", headers=BROWSER, data={
        "name": "Fulano", "email": "fulano@example.com", "cpf_cnpj": "12345678900", "plan": "anual",
        "billing_type": "CREDIT_CARD", "senha_acesso": "senha1234", "senha_confirmar": "senha1234",
    })

    assert resp.status_code == 200
    fake_posthog.alias.assert_called_once_with(previous_id=visitor, distinct_id="cliente-1")
