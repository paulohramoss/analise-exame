"""
Testes da fila de análise assíncrona (core/jobs.py) usando fakeredis — não
depende de um Redis real nem de um worker de verdade rodando em background.
"""

from unittest.mock import MagicMock

import pytest

from core import jobs


def test_is_async_enabled_false_without_redis_url(monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    assert jobs.is_async_enabled() is False


def test_is_async_enabled_true_with_redis_url(monkeypatch):
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "redis://localhost:6379")
    assert jobs.is_async_enabled() is True


def test_enqueue_analysis_returns_none_without_redis(monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setattr(jobs, "_redis_conn", None)
    monkeypatch.setattr(jobs, "_redis_conn_checked", False)
    assert jobs.enqueue_analysis(mode="trial") is None


def test_get_job_status_not_found_without_redis(monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setattr(jobs, "_redis_conn", None)
    monkeypatch.setattr(jobs, "_redis_conn_checked", False)
    assert jobs.get_job_status("job-que-nao-existe") == {"status": "not_found"}


def test_get_job_status_unknown_id_with_redis(fake_redis_conn):
    assert jobs.get_job_status("id-inexistente") == {"status": "not_found"}


def test_enqueue_and_process_job_end_to_end(fake_redis_conn, run_pending_jobs, monkeypatch):
    fake_result = {
        "success": True,
        "exam_type": "joelho",
        "analysis": "laudo de teste",
        "references_used": 1,
        "model_used": "gemini-2.5-flash",
        "num_images": 1,
        "usage": [{"model": "gemini-2.5-flash", "input_tokens": 100, "output_tokens": 50}],
        "cache_hit": False,
    }
    analyze_mock = MagicMock(return_value=dict(fake_result))
    monkeypatch.setattr("core.analyzer.analyze_exam_from_bytes", analyze_mock)
    salvar_analise_mock = MagicMock(return_value="analise-id-123")
    monkeypatch.setattr("core.db.salvar_analise", salvar_analise_mock)
    salvar_imagem_mock = MagicMock()
    monkeypatch.setattr("core.db.salvar_imagem_exame", salvar_imagem_mock)
    add_spend_mock = MagicMock()
    monkeypatch.setattr("core.cost_tracking.add_spend", add_spend_mock)

    job_id = jobs.enqueue_analysis(
        mode="trial",
        images=[(b"fake-bytes", "image/jpeg")],
        image_meta=[{"origem": "upload", "arquivo_original": "joelho.jpg", "dicom_metadata": None}],
        exam_filename="joelho.jpg",
        api_key="fake-gemini-key",
        user_description="dor no joelho",
        model_name="gemini-2.5-flash",
        anthropic_api_key="",
        exclude_gemini_cost=False,
        cost_scopes=[("trial", 3.0)],
        persist={"descricao_usuario": "dor no joelho", "ip_address": "127.0.0.1"},
    )
    assert job_id is not None
    assert jobs.get_job_status(job_id)["status"] == "queued"

    run_pending_jobs(fake_redis_conn)

    status = jobs.get_job_status(job_id)
    assert status["status"] == "finished"
    assert status["result"]["analysis"] == "laudo de teste"
    assert status["result"]["analise_id"] == "analise-id-123"

    analyze_mock.assert_called_once()
    salvar_analise_mock.assert_called_once()
    salvar_imagem_mock.assert_called_once()

    from core import cost_tracking
    expected_cost = cost_tracking.estimate_cost_usd("gemini-2.5-flash", 100, 50)
    add_spend_mock.assert_called_once_with("trial", pytest.approx(expected_cost))


def test_job_failure_is_reported_as_failed_status(fake_redis_conn, run_pending_jobs, monkeypatch):
    monkeypatch.setattr(
        "core.analyzer.analyze_exam_from_bytes",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    job_id = jobs.enqueue_analysis(
        mode="trial",
        images=[(b"fake-bytes", "image/jpeg")],
        image_meta=[{}],
        exam_filename="joelho.jpg",
        api_key="fake-gemini-key",
        user_description="",
        model_name="gemini-2.5-flash",
        anthropic_api_key="",
        exclude_gemini_cost=False,
        cost_scopes=[],
        persist={},
    )

    run_pending_jobs(fake_redis_conn)

    status = jobs.get_job_status(job_id)
    assert status["status"] == "failed"
    assert "error" in status


def test_exclude_gemini_cost_only_charges_claude_portion(fake_redis_conn, run_pending_jobs, monkeypatch):
    fake_result = {
        "success": True,
        "exam_type": "joelho",
        "analysis": "laudo",
        "references_used": 0,
        "model_used": "gemini-2.5-flash + claude-sonnet-4-6 -> consenso",
        "num_images": 1,
        "usage": [
            {"model": "gemini-2.5-flash", "input_tokens": 1_000_000, "output_tokens": 1_000_000},
            {"model": "claude-sonnet-4-6", "input_tokens": 1_000_000, "output_tokens": 1_000_000},
        ],
        "cache_hit": False,
    }
    monkeypatch.setattr("core.analyzer.analyze_exam_from_bytes", MagicMock(return_value=dict(fake_result)))
    monkeypatch.setattr("core.db.salvar_analise", MagicMock(return_value=None))
    monkeypatch.setattr("core.db.salvar_imagem_exame", MagicMock())
    add_spend_mock = MagicMock()
    monkeypatch.setattr("core.cost_tracking.add_spend", add_spend_mock)

    job_id = jobs.enqueue_analysis(
        mode="api",
        images=[(b"fake-bytes", "image/jpeg")],
        image_meta=[{}],
        exam_filename="joelho.jpg",
        api_key="chave-do-chamador",
        user_description="",
        model_name="gemini-2.5-flash",
        anthropic_api_key="server-claude-key",
        exclude_gemini_cost=True,
        cost_scopes=[("apikey:abc", 5.0)],
        persist={},
    )
    run_pending_jobs(fake_redis_conn)

    assert jobs.get_job_status(job_id)["status"] == "finished"
    add_spend_mock.assert_called_once()
    scope, cost_usd = add_spend_mock.call_args[0]
    assert scope == "apikey:abc"

    from core import cost_tracking
    claude_only_cost = cost_tracking.estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost_usd == pytest.approx(claude_only_cost)
