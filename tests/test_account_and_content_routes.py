"""
Testes de integração das rotas de conta (/acesso, /premium/logout,
/login/definir-senha) e de conteúdo premium (/laudos, /clinica/dashboard,
/feedback, /validacao-clinica) e páginas públicas (/planos, /checkout,
/checkout/pending).

core.db fica com persistência desativada por padrão (SUPABASE_URL vazio, ver
conftest.py) — listagens retornam [] e buscas retornam None sem precisar de
mock; quando o teste depende de um retorno específico, a função é mockada.
"""

from unittest.mock import MagicMock

import app as flask_app_module


def _login_as_admin(client):
    key = flask_app_module._get_admin_key()
    resp = client.get(f"/admin/entrar?key={key}")
    assert resp.status_code in (302, 303)


# ── /acesso ───────────────────────────────────────────────────────────────────

def test_acesso_get_renders(client):
    resp = client.get("/acesso")
    assert resp.status_code == 200


def test_acesso_get_redirects_when_already_premium(client, monkeypatch):
    monkeypatch.setattr(flask_app_module, "is_premium", lambda req: True)
    resp = client.get("/acesso")
    assert resp.status_code == 302


def test_acesso_verificar_invalid_email_redirects(client):
    resp = client.post("/acesso/verificar", data={"email": "sem-arroba"})
    assert resp.status_code == 302
    assert "/acesso" in resp.headers["Location"]


def test_acesso_verificar_no_confirmed_payment_redirects(client, monkeypatch):
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_confirmado_por_email", lambda email: None)

    resp = client.post("/acesso/verificar", data={"email": "sem-pagamento@example.com"})

    assert resp.status_code == 302
    assert "/acesso" in resp.headers["Location"]


def test_acesso_verificar_with_password_set_sets_premium_cookie(client, monkeypatch):
    monkeypatch.setattr(
        flask_app_module.db, "buscar_pagamento_confirmado_por_email",
        lambda email: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(flask_app_module.db, "buscar_senha_hash_cliente", lambda email: "hash-existente")
    monkeypatch.setattr(flask_app_module.db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_sessao", MagicMock())

    resp = client.post("/acesso/verificar", data={"email": "cliente@example.com"})

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    assert flask_app_module.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


def test_acesso_verificar_without_password_redirects_to_definir_senha(client, monkeypatch):
    monkeypatch.setattr(
        flask_app_module.db, "buscar_pagamento_confirmado_por_email",
        lambda email: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(flask_app_module.db, "buscar_senha_hash_cliente", lambda email: None)
    monkeypatch.setattr(flask_app_module.db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_sessao", MagicMock())

    resp = client.post("/acesso/verificar", data={"email": "cliente@example.com"})

    assert resp.status_code == 302
    assert "/login/definir-senha" in resp.headers["Location"]


# ── /premium/logout ───────────────────────────────────────────────────────────

def test_premium_logout_removes_cookie_and_redirects_to_login(client):
    resp = client.get("/premium/logout")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert flask_app_module.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


# ── /login/definir-senha ──────────────────────────────────────────────────────

def test_login_definir_senha_invalid_token_redirects_to_login(client):
    resp = client.get("/login/definir-senha?t=token-invalido")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_definir_senha_get_renders_with_valid_token(client):
    token = flask_app_module.generate_setup_token("cliente@example.com")
    resp = client.get(f"/login/definir-senha?t={token}")
    assert resp.status_code == 200


def test_login_definir_senha_post_short_password_shows_error(client):
    token = flask_app_module.generate_setup_token("cliente@example.com")
    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "123", "confirmar": "123"}
    )
    assert resp.status_code == 200
    assert "8 caracteres" in resp.get_data(as_text=True)


def test_login_definir_senha_post_mismatch_shows_error(client):
    token = flask_app_module.generate_setup_token("cliente@example.com")
    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "outra-senha"}
    )
    assert resp.status_code == 200
    assert "não coincidem" in resp.get_data(as_text=True)


def test_login_definir_senha_post_success_without_active_plan_redirects_to_login(client, monkeypatch):
    token = flask_app_module.generate_setup_token("cliente@example.com")
    monkeypatch.setattr(flask_app_module.db, "salvar_senha_cliente", lambda email, senha_hash: True)
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_confirmado_por_email", lambda email: None)

    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "senha1234"}
    )

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"


def test_login_definir_senha_post_success_with_active_plan_sets_premium_cookie(client, monkeypatch):
    token = flask_app_module.generate_setup_token("cliente@example.com")
    monkeypatch.setattr(flask_app_module.db, "salvar_senha_cliente", lambda email, senha_hash: True)
    monkeypatch.setattr(
        flask_app_module.db, "buscar_pagamento_confirmado_por_email",
        lambda email: ("pay_1", "cliente@example.com|anual"),
    )
    monkeypatch.setattr(flask_app_module.db, "buscar_cliente_id_por_email", lambda email: "cliente-1")
    monkeypatch.setattr(flask_app_module.db, "buscar_pagamento_id", lambda payment_id: "pagamento-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_sessao", MagicMock())

    resp = client.post(
        "/login/definir-senha", data={"t": token, "senha": "senha1234", "confirmar": "senha1234"}
    )

    assert resp.status_code == 302
    assert flask_app_module.PREMIUM_COOKIE in resp.headers.get("Set-Cookie", "")


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
        flask_app_module.db, "buscar_analise",
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
        flask_app_module.db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False: {"analise_completa": "laudo", "tipo_exame": "joelho"},
    )
    monkeypatch.setattr(flask_app_module.db, "salvar_feedback", lambda **kwargs: "feedback-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_diagnostico_validado", lambda **kwargs: "diag-1")

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
        flask_app_module.db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False: {"analise_completa": "laudo", "tipo_exame": "joelho"},
    )
    monkeypatch.setattr(flask_app_module.db, "salvar_diagnostico_validado", lambda **kwargs: "diag-1")
    monkeypatch.setattr(flask_app_module.db, "salvar_feedback", lambda **kwargs: "feedback-1")

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
    monkeypatch.setattr(flask_app_module, "is_premium", lambda req: True)
    resp = client.get("/checkout")
    assert resp.status_code == 302


def test_checkout_pending_without_payment_id_redirects_to_checkout(client):
    resp = client.get("/checkout/pending")
    assert resp.status_code == 302
    assert "/checkout" in resp.headers["Location"]


def test_checkout_pending_renders_with_payment_id(client):
    resp = client.get("/checkout/pending?payment_id=pay_1&invoice_url=https://asaas.example/pay_1")
    assert resp.status_code == 200
