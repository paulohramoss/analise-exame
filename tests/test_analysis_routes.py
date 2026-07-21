"""
Testes de integração das rotas de análise no caminho síncrono (sem fila Redis
configurada — is_async_enabled() é False por padrão no ambiente de teste, ver
conftest.py). Cobre /analyze, /trial/analyze e /api/analyze: validações de
entrada, bloqueio por usuário não-premium, e o caminho feliz síncrono
(analyze_exam_from_bytes mockado, sem chamadas reais de IA).

O caminho assíncrono (fila Redis via fakeredis) já é coberto por
tests/test_async_routes.py.
"""

import io
from unittest.mock import MagicMock

import app as flask_app_module

FAKE_RESULT = {
    "success": True,
    "exam_type": "joelho",
    "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\nlaudo gerado de forma síncrona",
    "references_used": 0,
    "model_used": "gemini-2.5-flash",
    "num_images": 1,
    "usage": [],
    "cache_hit": False,
}


def _mock_analyze(monkeypatch, result=None):
    # app.py importa analyze_exam_from_bytes diretamente no seu namespace no
    # carregamento do módulo (from core.analyzer import ...), então o caminho
    # síncrono das rotas precisa ser mockado em flask_app_module, não em
    # core.analyzer (isso só afetaria o worker assíncrono, que reimporta em
    # tempo de execução — ver core/jobs.py).
    monkeypatch.setattr(
        flask_app_module, "analyze_exam_from_bytes",
        MagicMock(return_value=dict(result or FAKE_RESULT)),
    )


def _upload_payload(**extra):
    payload = {"exam_image": (io.BytesIO(b"fake-image-bytes"), "joelho.jpg")}
    payload.update(extra)
    return payload


def _login_as_admin(client):
    key = flask_app_module._get_admin_key()
    resp = client.get(f"/admin/entrar?key={key}")
    assert resp.status_code in (302, 303)


# ── /analyze (premium) ────────────────────────────────────────────────────────

def test_analyze_requires_premium(client):
    resp = client.post("/analyze", data=_upload_payload(privacy_consent="accepted"))
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_analyze_missing_privacy_consent_redirects(client):
    _login_as_admin(client)
    resp = client.post("/analyze", data=_upload_payload())
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_analyze_missing_responsible_info_redirects(client):
    _login_as_admin(client)
    resp = client.post("/analyze", data=_upload_payload(privacy_consent="accepted"))
    assert resp.status_code == 302


def test_analyze_no_image_redirects(client):
    _login_as_admin(client)
    resp = client.post(
        "/analyze",
        data={
            "privacy_consent": "accepted",
            "responsible_name": "Dra. Ana",
            "responsible_role": "Ortopedista",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302


def test_analyze_sync_success_renders_result(client, monkeypatch):
    _mock_analyze(monkeypatch)
    _login_as_admin(client)

    resp = client.post(
        "/analyze",
        data=_upload_payload(
            privacy_consent="accepted",
            responsible_name="Dra. Ana",
            responsible_role="Ortopedista",
            description="dor no joelho",
        ),
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200
    assert "laudo gerado de forma síncrona" in resp.get_data(as_text=True)


def test_analyze_sync_analyzer_failure_flashes_and_redirects(client, monkeypatch):
    monkeypatch.setattr(
        flask_app_module, "analyze_exam_from_bytes",
        MagicMock(side_effect=Exception("boom")),
    )
    _login_as_admin(client)

    resp = client.post(
        "/analyze",
        data=_upload_payload(
            privacy_consent="accepted",
            responsible_name="Dra. Ana",
            responsible_role="Ortopedista",
        ),
        content_type="multipart/form-data",
    )

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


# ── /trial e /trial/analyze ───────────────────────────────────────────────────

def test_trial_get_renders_trial_page(client):
    resp = client.get("/trial")
    assert resp.status_code == 200
    assert b"trial-form" in resp.data


def test_trial_analyze_missing_privacy_consent_returns_400(client):
    resp = client.post("/trial/analyze", data=_upload_payload())
    assert resp.status_code == 400


def test_trial_analyze_missing_image_returns_400(client):
    resp = client.post("/trial/analyze", data={"privacy_consent": "accepted"})
    assert resp.status_code == 400


def test_trial_analyze_invalid_extension_returns_400(client):
    resp = client.post(
        "/trial/analyze",
        data={
            "privacy_consent": "accepted",
            "exam_image": (io.BytesIO(b"not-an-image"), "arquivo.txt"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_trial_analyze_sync_success(client, monkeypatch):
    _mock_analyze(monkeypatch)

    resp = client.post(
        "/trial/analyze",
        data=_upload_payload(privacy_consent="accepted", description="dor no joelho"),
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert "laudo gerado de forma síncrona" in body["analysis"]
    assert "consensus" in body


# ── /api/analyze ──────────────────────────────────────────────────────────────

def test_api_analyze_no_image_returns_400(client):
    resp = client.post("/api/analyze", data={}, headers={"X-API-Key": "chave"})
    assert resp.status_code == 400


def test_api_analyze_unsupported_extension_returns_400(client):
    resp = client.post(
        "/api/analyze",
        data={"exam_image": (io.BytesIO(b"not-an-image"), "arquivo.txt")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "chave"},
    )
    assert resp.status_code == 400


def test_api_analyze_sync_success(client, monkeypatch):
    _mock_analyze(monkeypatch)

    resp = client.post(
        "/api/analyze",
        data=_upload_payload(description="joelho direito"),
        content_type="multipart/form-data",
        headers={"X-API-Key": "chave-do-chamador"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert "laudo gerado de forma síncrona" in body["analysis"]
    assert "consensus" in body
