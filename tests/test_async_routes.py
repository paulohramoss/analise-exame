"""
Testes de integração das rotas de análise quando a fila assíncrona está
disponível (fake_redis_conn) — cobre o caminho "feliz" de enfileirar,
processar com um worker síncrono (burst mode) e consultar o resultado.

O fallback síncrono (sem Redis) já é exercitado pelo resto da suíte, já que
os testes não habilitam fila por padrão.
"""

import io
from unittest.mock import MagicMock

import app as flask_app_module

FAKE_RESULT = {
    "success": True,
    "exam_type": "joelho",
    "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\nlaudo gerado de forma assíncrona",
    "references_used": 0,
    "model_used": "gemini-2.5-flash",
    "num_images": 1,
    "usage": [],
    "cache_hit": False,
}


def _mock_analyze(monkeypatch, result=None):
    monkeypatch.setattr(
        "core.analyzer.analyze_exam_from_bytes",
        MagicMock(return_value=dict(result or FAKE_RESULT)),
    )


def _upload_payload(**extra):
    payload = {"exam_image": (io.BytesIO(b"fake-image-bytes"), "joelho.jpg")}
    payload.update(extra)
    return payload


def _login_as_admin(client):
    """Usa o cookie admin (bypassa o fluxo de pagamento) para acessar rotas premium."""
    key = flask_app_module._get_admin_key()
    resp = client.get(f"/admin/entrar?key={key}")
    assert resp.status_code in (302, 303)


def test_trial_analyze_enqueues_and_finishes(client, fake_redis_conn, run_pending_jobs, monkeypatch):
    _mock_analyze(monkeypatch)

    resp = client.post(
        "/trial/analyze",
        data=_upload_payload(description="dor no joelho", privacy_consent="accepted"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued"
    assert body["job_id"]

    run_pending_jobs(fake_redis_conn)

    status_resp = client.get(body["status_url"])
    assert status_resp.status_code == 200
    status_body = status_resp.get_json()
    assert status_body["status"] == "finished"
    assert "laudo gerado de forma assíncrona" in status_body["analysis"]
    assert status_body["exam_type"] == "Joelho"
    assert "consensus" in status_body


def test_trial_analyze_status_not_found(client, fake_redis_conn):
    resp = client.get("/trial/analyze/status/job-que-nao-existe")
    assert resp.status_code == 404
    assert resp.get_json()["status"] == "not_found"


def test_api_analyze_enqueues_and_finishes(client, fake_redis_conn, run_pending_jobs, monkeypatch):
    _mock_analyze(monkeypatch)

    resp = client.post(
        "/api/analyze",
        data=_upload_payload(description="joelho direito"),
        content_type="multipart/form-data",
        headers={"X-API-Key": "chave-do-chamador"},
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued"

    run_pending_jobs(fake_redis_conn)

    status_resp = client.get(body["status_url"])
    status_body = status_resp.get_json()
    assert status_body["status"] == "finished"
    assert "laudo gerado de forma assíncrona" in status_body["analysis"]
    assert "consensus" in status_body


def test_premium_analyze_redirects_to_processing_then_result(
    client, fake_redis_conn, run_pending_jobs, monkeypatch
):
    _mock_analyze(monkeypatch)
    _login_as_admin(client)

    resp = client.post(
        "/analyze",
        data=_upload_payload(
            description="dor no joelho",
            privacy_consent="accepted",
            responsible_name="Dra. Ana",
            responsible_role="Ortopedista",
        ),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    processing_url = resp.headers["Location"]
    assert "/analyze/processando/" in processing_url

    job_id = processing_url.rstrip("/").rsplit("/", 1)[-1]

    # Antes do worker rodar, o status ainda não terminou.
    status_resp = client.get(f"/analyze/status/{job_id}")
    assert status_resp.get_json()["status"] in ("queued", "started")

    run_pending_jobs(fake_redis_conn)

    status_resp = client.get(f"/analyze/status/{job_id}")
    assert status_resp.get_json()["status"] == "finished"

    resultado_resp = client.get(f"/analyze/resultado/{job_id}")
    assert resultado_resp.status_code == 200
    assert "laudo gerado de forma assíncrona" in resultado_resp.get_data(as_text=True)


def test_premium_resultado_redirects_back_to_processing_if_not_finished(client, fake_redis_conn):
    _login_as_admin(client)
    resp = client.get("/analyze/resultado/job-nao-processado-ainda")
    assert resp.status_code == 302
    assert "/analyze/processando/" in resp.headers["Location"]
