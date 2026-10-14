"""
Testes de integração das rotas de conta (/acesso, /premium/logout,
/login/definir-senha) e de conteúdo premium (/laudos, /clinica/dashboard,
/feedback, /validacao-clinica) e páginas públicas (/planos, /checkout,
/checkout/pending).

core.db fica com persistência desativada por padrão (SUPABASE_URL vazio, ver
conftest.py) — listagens retornam [] e buscas retornam None sem precisar de
mock; quando o teste depende de um retorno específico, a função é mockada.
"""

import re
from unittest.mock import MagicMock

import app as flask_app_module
from web.routes import auth as auth_routes
from web import config
from core import db
import flask
from core import mailer
from web import security


def _login_as_admin(client):
    key = config.get_admin_key()
    resp = client.get(f"/admin/entrar?key={key}")
    assert resp.status_code in (302, 303)


# ── /acesso ───────────────────────────────────────────────────────────────────

def test_acesso_get_renders(client):
    resp = client.get("/acesso")
    assert resp.status_code == 200


def test_acesso_get_redirects_when_already_premium(client, monkeypatch):
    monkeypatch.setattr(security, "is_premium", lambda req: True)
    resp = client.get("/acesso")
    assert resp.status_code == 302


def test_acesso_verificar_invalid_email_redirects(client):
    resp = client.post("/acesso/verificar", data={"email": "sem-arroba"})
    assert resp.status_code == 302
    assert "/acesso" in resp.headers["Location"]


def _mock_mailer(monkeypatch, configured=True):
    sent = []
    monkeypatch.setattr(mailer, "is_configured", lambda: configured)
    monkeypatch.setattr(
        mailer, "send_email",
        lambda to, subject, body: sent.append({"to": to, "subject": subject, "body": body}) or True,
    )
    monkeypatch.setenv("APP_BASE_URL", "https://app.threehealth.test")
    return sent


def test_acesso_verificar_no_confirmed_payment_sends_nothing(client, monkeypatch):
    sent = _mock_mailer(monkeypatch)
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)

    resp = client.post("/acesso/verificar", data={"email": "sem-pagamento@example.com"})

    assert resp.status_code == 302
    assert "/acesso" in resp.headers["Location"]
    assert sent == []


def test_acesso_verificar_never_grants_access_directly(client, monkeypatch):
    """Saber o e-mail de um cliente não basta: o acesso só vem pelo link enviado ao e-mail."""
    sent = _mock_mailer(monkeypatch)
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: None)

    resp = client.post("/acesso/verificar", data={"email": "cliente@example.com"})

    assert resp.status_code == 302
    assert "/acesso" in resp.headers["Location"]
    assert "/login/definir-senha" not in resp.headers["Location"]
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")
    assert len(sent) == 1
    assert sent[0]["to"] == "cliente@example.com"
    assert "https://app.threehealth.test/login/definir-senha?t=" in sent[0]["body"]


def test_acesso_verificar_link_ignores_forged_host_header(client, monkeypatch):
    sent = _mock_mailer(monkeypatch)
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )

    client.post(
        "/acesso/verificar", data={"email": "cliente@example.com"}, headers={"Host": "atacante.example"}
    )

    assert "atacante.example" not in sent[0]["body"]


def test_acesso_verificar_same_response_with_or_without_plan(client, monkeypatch):
    _mock_mailer(monkeypatch)
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", f"{email}|anual") if email == "cliente@example.com" else None,
    )

    with_plan = client.post("/acesso/verificar", data={"email": "cliente@example.com"}, follow_redirects=True)
    without_plan = client.post("/acesso/verificar", data={"email": "outro@example.com"}, follow_redirects=True)

    assert auth_routes.ACESSO_LINK_ENVIADO in with_plan.get_data(as_text=True)
    assert auth_routes.ACESSO_LINK_ENVIADO in without_plan.get_data(as_text=True)


def test_acesso_verificar_without_smtp_does_not_grant_access(client, monkeypatch):
    sent = _mock_mailer(monkeypatch, configured=False)
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )

    resp = client.post("/acesso/verificar", data={"email": "cliente@example.com"})

    assert resp.status_code == 302
    assert "/acesso" in resp.headers["Location"]
    assert security.PREMIUM_COOKIE not in resp.headers.get("Set-Cookie", "")
    assert sent == []


# ── /premium/logout ───────────────────────────────────────────────────────────

def test_premium_logout_removes_cookie_and_redirects_to_login(client):
    resp = client.get("/premium/logout")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert security.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


# ── /login/definir-senha ──────────────────────────────────────────────────────

def test_login_definir_senha_invalid_token_redirects_to_login(client):
    resp = client.get("/login/definir-senha?t=token-invalido")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_definir_senha_get_renders_with_valid_token(client):
    token = security.generate_setup_token("cliente@example.com")
    resp = client.get(f"/login/definir-senha?t={token}")
    assert resp.status_code == 200


def test_login_definir_senha_post_short_password_shows_error(client):
    token = security.generate_setup_token("cliente@example.com")
    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "123", "confirmar": "123"}
    )
    assert resp.status_code == 200
    assert "8 caracteres" in resp.get_data(as_text=True)


def test_login_definir_senha_post_mismatch_shows_error(client):
    token = security.generate_setup_token("cliente@example.com")
    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "outra-senha"}
    )
    assert resp.status_code == 200
    assert "não coincidem" in resp.get_data(as_text=True)


def test_login_definir_senha_post_success_without_active_plan_redirects_to_login(client, monkeypatch):
    token = security.generate_setup_token("cliente@example.com")
    monkeypatch.setattr(db, "salvar_senha_cliente", lambda email, senha_hash: True)
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: None)

    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "senha1234"}
    )

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"


def test_login_definir_senha_post_success_with_active_plan_sets_premium_cookie(client, monkeypatch):
    token = security.generate_setup_token("cliente@example.com")
    monkeypatch.setattr(db, "salvar_senha_cliente", lambda email, senha_hash: True)
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(db, "salvar_sessao", MagicMock())

    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "senha1234"}
    )

    assert resp.status_code == 302
    assert security.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


def test_setup_token_is_single_use(monkeypatch):
    """Depois que a senha muda, o mesmo link de definição deixa de valer."""
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: None)
    token = security.generate_setup_token("cliente@example.com")
    assert security.verify_setup_token(token) == "cliente@example.com"

    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: "hash-novo")
    assert security.verify_setup_token(token) is None


def test_setup_token_without_password_fingerprint_is_rejected():
    legacy = security.serializer().dumps(
        {"email": "cliente@example.com"}, salt=security.SETUP_SALT
    )
    assert security.verify_setup_token(legacy) is None


# ── /laudos ───────────────────────────────────────────────────────────────────

def test_laudos_requires_premium(client):
    resp = client.get("/laudos")
    assert resp.status_code == 302


def test_laudos_renders_for_premium_user(client):
    _login_as_admin(client)
    resp = client.get("/laudos")
    assert resp.status_code == 200


def test_laudo_detalhe_not_found_redirects_to_laudos(client):
    _login_as_admin(client)
    resp = client.get("/laudos/inexistente")
    assert resp.status_code == 302
    assert "/laudos" in resp.headers["Location"]


def test_laudo_detalhe_found_renders(client, monkeypatch):
    _login_as_admin(client)
    monkeypatch.setattr(
        db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False: {
            "id": "analise-1",
            "analise_completa": "laudo salvo",
            "tipo_exame": "joelho",
            "modelo_ia": "gemini-2.5-flash",
            "descricao_usuario": "dor no joelho",
        },
    )

    resp = client.get("/laudos/analise-1")

    assert resp.status_code == 200
    assert "laudo salvo" in resp.get_data(as_text=True)


def test_laudo_detalhe_escapes_html_from_ai_output(client, monkeypatch):
    """O texto do laudo vem da IA (influenciável por prompt injection): nunca vira HTML."""
    _login_as_admin(client)
    payload = '<img src=x onerror="alert(document.cookie)"><script>alert(1)</script>'
    monkeypatch.setattr(
        db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False: {
            "id": "analise-1",
            "analise_completa": f"## 1. ACHADOS\n{payload}",
            "tipo_exame": "joelho",
            "modelo_ia": "gemini-2.5-flash",
        },
    )

    html = client.get("/laudos/analise-1").get_data(as_text=True)

    assert payload not in html
    assert "&lt;img src=x onerror=" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ── /clinica/dashboard ────────────────────────────────────────────────────────

def test_dashboard_clinica_requires_premium(client):
    resp = client.get("/clinica/dashboard")
    assert resp.status_code == 302


def test_dashboard_clinica_renders_with_empty_data(client):
    _login_as_admin(client)
    resp = client.get("/clinica/dashboard")
    assert resp.status_code == 200


# ── /feedback ─────────────────────────────────────────────────────────────────

def test_feedback_requires_premium(client):
    resp = client.post("/feedback", json={"analysis_id": "a1", "feedback": "agree"})
    assert resp.status_code == 302


def test_feedback_invalid_type_returns_400(client):
    _login_as_admin(client)
    resp = client.post("/feedback", json={"analysis_id": "a1", "feedback": "invalido"})
    assert resp.status_code == 400


def test_feedback_missing_analysis_id_returns_400(client):
    _login_as_admin(client)
    resp = client.post("/feedback", json={"feedback": "agree"})
    assert resp.status_code == 400


def test_feedback_partial_without_comment_returns_400(client):
    _login_as_admin(client)
    resp = client.post("/feedback", json={"analysis_id": "a1", "feedback": "partial"})
    assert resp.status_code == 400


def test_feedback_analysis_not_found_returns_404(client):
    _login_as_admin(client)
    resp = client.post("/feedback", json={"analysis_id": "inexistente", "feedback": "agree"})
    assert resp.status_code == 404


def test_feedback_success(client, monkeypatch):
    _login_as_admin(client)
    monkeypatch.setattr(
        db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False: {"analise_completa": "laudo", "tipo_exame": "joelho"},
    )
    monkeypatch.setattr(db, "salvar_feedback", lambda **kwargs: "feedback-1")
    monkeypatch.setattr(db, "salvar_diagnostico_validado", lambda **kwargs: "diag-1")

    resp = client.post("/feedback", json={"analysis_id": "a1", "feedback": "agree"})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


# ── /validacao-clinica ────────────────────────────────────────────────────────

def test_validacao_clinica_requires_premium(client):
    resp = client.post("/validacao-clinica", json={"analysis_id": "a1", "final_diagnosis": "ok"})
    assert resp.status_code == 302


def test_validacao_clinica_missing_diagnosis_returns_400(client):
    _login_as_admin(client)
    resp = client.post("/validacao-clinica", json={"analysis_id": "a1"})
    assert resp.status_code == 400


def test_validacao_clinica_analysis_not_found_returns_404(client):
    _login_as_admin(client)
    resp = client.post(
        "/validacao-clinica", json={"analysis_id": "inexistente", "final_diagnosis": "diagnóstico final"}
    )
    assert resp.status_code == 404


def test_validacao_clinica_success_json(client, monkeypatch):
    _login_as_admin(client)
    monkeypatch.setattr(
        db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False: {"analise_completa": "laudo", "tipo_exame": "joelho"},
    )
    monkeypatch.setattr(db, "salvar_diagnostico_validado", lambda **kwargs: "diag-1")
    monkeypatch.setattr(db, "salvar_feedback", lambda **kwargs: "feedback-1")

    resp = client.post(
        "/validacao-clinica",
        json={"analysis_id": "a1", "final_diagnosis": "diagnóstico final", "concordance_score": 90},
    )

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


# ── Home e página de espera assíncrona ────────────────────────────────────────

def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_static_assets_have_content_hash(client):
    """CSS/JS em /static ficam em cache por 1 ano; a URL precisa mudar quando o arquivo muda."""
    html = client.get("/").get_data(as_text=True)
    assert re.search(r'/static/base\.css\?v=[0-9a-f]{10}"', html)
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{10}"', html)
    assert re.search(r'/static/ui\.js\?v=[0-9a-f]{10}"', html)


def _local_runtime(monkeypatch, *, testing=False):
    for var in ("VERCEL", "VERCEL_ENV", "FLASK_ENV", "ENV", "APP_ENV", "LOCAL_PREMIUM_BYPASS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(flask_app_module.app, "testing", testing)


def test_localhost_gets_premium_access_in_local_runtime(client, monkeypatch):
    _local_runtime(monkeypatch)
    with flask_app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert security.is_premium(flask.request) is True


def test_local_bypass_ignores_remote_clients(client, monkeypatch):
    _local_runtime(monkeypatch)
    with flask_app_module.app.test_request_context(
        "/", environ_base={"REMOTE_ADDR": "203.0.113.7"}, headers={"Host": "localhost:5000"}
    ):
        assert security.is_premium(flask.request) is False


def test_local_bypass_disabled_on_vercel_and_production(client, monkeypatch):
    _local_runtime(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    with flask_app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert security.is_premium(flask.request) is False

    monkeypatch.delenv("VERCEL")
    monkeypatch.setenv("VERCEL_ENV", "production")
    with flask_app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert security.is_premium(flask.request) is False


def test_local_bypass_can_be_turned_off(client, monkeypatch):
    _local_runtime(monkeypatch)
    monkeypatch.setenv("LOCAL_PREMIUM_BYPASS", "0")
    with flask_app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert security.is_premium(flask.request) is False


def test_analyze_processando_requires_premium(client):
    resp = client.get("/analyze/processando/job-1")
    assert resp.status_code == 302


def test_analyze_processando_renders_for_premium_user(client):
    _login_as_admin(client)
    resp = client.get("/analyze/processando/job-1")
    assert resp.status_code == 200


# ── Páginas públicas ──────────────────────────────────────────────────────────

def test_planos_renders(client):
    resp = client.get("/planos")
    assert resp.status_code == 200


def test_checkout_get_renders(client):
    resp = client.get("/checkout")
    assert resp.status_code == 200


def test_checkout_get_redirects_when_already_premium(client, monkeypatch):
    monkeypatch.setattr(security, "is_premium", lambda req: True)
    resp = client.get("/checkout")
    assert resp.status_code == 302


def test_checkout_pending_without_payment_id_redirects_to_checkout(client):
    resp = client.get("/checkout/pending")
    assert resp.status_code == 302
    assert "/checkout" in resp.headers["Location"]


def test_checkout_pending_renders_with_payment_id(client):
    resp = client.get("/checkout/pending?payment_id=pay_1&invoice_url=https://asaas.example/pay_1")
    assert resp.status_code == 200
