"""
Testes de endurecimento de segurança: IP do cliente, validade do plano,
revogação de sessão, webhook do Asaas, CSRF por origem e cabeçalhos HTTP.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from werkzeug.security import generate_password_hash

import app as flask_app_module
from core import db
from web import security


# ── IP do cliente (rate limit / orçamento) ────────────────────────────────────

def _ip_for(headers, remote_addr="10.0.0.1"):
    with flask_app_module.app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": remote_addr}):
        return security.get_ip()


def test_get_ip_ignores_client_supplied_forwarded_for_by_default(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    assert _ip_for({"X-Forwarded-For": "1.2.3.4"}) == "10.0.0.1"


def test_get_ip_uses_rightmost_hop_appended_by_trusted_proxy(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    # "1.2.3.4" foi forjado pelo cliente; "198.51.100.7" foi anexado pelo nosso proxy.
    assert _ip_for({"X-Forwarded-For": "1.2.3.4, 198.51.100.7"}) == "198.51.100.7"


def test_get_ip_on_vercel_uses_platform_header(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert _ip_for({"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "203.0.113.9"}) == "203.0.113.9"


def test_spoofed_forwarded_for_does_not_bypass_rate_limit(client, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)
    statuses = [
        client.post(
            "/login",
            data={"email": "alvo@example.com", "senha": "x"},
            headers={"X-Forwarded-For": f"10.9.8.{i}"},
        ).status_code
        for i in range(11)
    ]
    assert statuses[-1] == 429


# ── Validade do plano ─────────────────────────────────────────────────────────

def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _mock_login_db(monkeypatch, inicio_iso, plano="mensal"):
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", f"{email}|{plano}", inicio_iso),
    )
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: generate_password_hash("senha-correta"))
    monkeypatch.setattr(db, "salvar_sessao", MagicMock())


def test_login_rejects_expired_plan(client, monkeypatch):
    _mock_login_db(monkeypatch, _iso_days_ago(45), plano="mensal")  # mensal = 30 dias

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "senha-correta"})

    assert resp.status_code == 200
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")


def test_login_cookie_lasts_only_until_plan_end(client, monkeypatch):
    _mock_login_db(monkeypatch, _iso_days_ago(20), plano="mensal")  # restam ~10 dias

    resp = client.post("/login", data={"email": "cliente@example.com", "senha": "senha-correta"})

    assert resp.status_code == 302
    cookie = next(h for h in resp.headers.getlist("Set-Cookie") if h.startswith(security.PREMIUM_COOKIE))
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert 9 * 86400 < max_age <= 10 * 86400 + 60


def test_plan_start_prefers_asaas_confirmation_date():
    row = {
        "created_at": "2026-01-01T10:00:00+00:00",
        "payload_asaas": {"event": "PAYMENT_CONFIRMED", "payment": {"confirmedDate": "2026-01-04"}},
    }
    assert db._inicio_do_plano(row) == "2026-01-04"
    assert db._inicio_do_plano({"created_at": "2026-01-01T10:00:00+00:00"}) == "2026-01-01T10:00:00+00:00"


# ── Revogação de sessão ───────────────────────────────────────────────────────

def test_revoked_session_version_loses_premium_access(client, monkeypatch):
    token = security.generate_premium_token("pay_1", "cliente@example.com", 3600, session_version=0)
    client.set_cookie(security.PREMIUM_COOKIE, token)

    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: {"id": "c1", "sessao_versao": 0})
    assert client.get("/laudos").status_code == 200

    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: {"id": "c1", "sessao_versao": 1})
    resp = client.get("/laudos")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_password_change_revokes_other_sessions(client, monkeypatch):
    token = security.generate_setup_token("cliente@example.com")
    revogar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "revogar_sessoes_cliente", revogar)
    monkeypatch.setattr(db, "salvar_senha_cliente", lambda email, senha_hash: True)
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)

    client.post("/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "senha1234"})

    revogar.assert_called_once_with("cliente@example.com")


# ── Webhook do Asaas ──────────────────────────────────────────────────────────

def test_webhook_without_token_is_refused_outside_local(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "")
    monkeypatch.setenv("VERCEL", "1")
    salvar = MagicMock()
    monkeypatch.setattr(db, "atualizar_status_pagamento", salvar)

    resp = client.post("/webhook/asaas", json={"event": "PAYMENT_CONFIRMED", "payment": {"id": "p", "status": "CONFIRMED"}})

    assert resp.status_code == 503
    salvar.assert_not_called()


def test_webhook_refund_revokes_customer_sessions(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    monkeypatch.setattr(db, "salvar_webhook", MagicMock())
    monkeypatch.setattr(db, "atualizar_status_pagamento", MagicMock())
    revogar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "revogar_sessoes_cliente", revogar)

    resp = client.post(
        "/webhook/asaas",
        json={
            "event": "PAYMENT_REFUNDED",
            "payment": {"id": "pay_1", "status": "REFUNDED", "externalReference": "Cliente@Example.com|anual"},
        },
        headers={"asaas-access-token": "segredo"},
    )

    assert resp.status_code == 200
    revogar.assert_called_once_with("cliente@example.com")


# ── CSRF por origem ───────────────────────────────────────────────────────────

def test_cross_site_post_is_refused(client):
    resp = client.post("/login", data={"email": "a@example.com", "senha": "x"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_null_origin_post_is_refused(client):
    resp = client.post("/login", data={"email": "a@example.com", "senha": "x"}, headers={"Origin": "null"})
    assert resp.status_code == 403


def test_same_origin_post_is_accepted_even_if_scheme_differs(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)
    # O proxy termina TLS: o Flask vê http://localhost, o navegador manda Origin https://localhost.
    resp = client.post("/login", data={"email": "a@example.com", "senha": "x"}, headers={"Origin": "https://localhost"})
    assert resp.status_code == 200


def test_cross_site_referer_without_origin_is_refused(client):
    resp = client.post("/feedback", json={}, headers={"Referer": "https://evil.example/pagina"})
    assert resp.status_code == 403


def test_webhook_and_api_are_exempt_from_origin_check(client, monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "")
    monkeypatch.setattr(db, "salvar_webhook", MagicMock())
    resp = client.post("/webhook/asaas", json={}, headers={"Origin": "https://asaas.com"})
    assert resp.status_code == 200
    resp = client.post("/api/analyze", data={}, headers={"Origin": "https://integrador.example", "X-API-Key": "chave"})
    assert resp.status_code == 400  # passou da checagem de origem; falhou só por faltar imagem


# ── Cabeçalhos de segurança ───────────────────────────────────────────────────

def test_security_headers_on_regular_pages(client):
    resp = client.get("/login")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]
    assert "object-src 'none'" in resp.headers["Content-Security-Policy"]


def test_trial_frame_ancestors_follow_configuration(client, monkeypatch):
    monkeypatch.setenv("TRIAL_FRAME_ANCESTORS", "https://threehealth.com.br")
    csp = client.get("/trial").headers["Content-Security-Policy"]
    assert "frame-ancestors 'self' https://threehealth.com.br" in csp
