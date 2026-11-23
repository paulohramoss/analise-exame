"""Operação: cache compartilhado, monitor de erros de IA, fila com prioridade e retentativa."""

from unittest.mock import MagicMock

import rq

from core import ai_monitor, analyzer, cost_tracking, jobs, mailer, pubmed, shared_cache


# ── Cache compartilhado ───────────────────────────────────────────────────────

def test_cache_compartilhado_sem_redis_e_noop(monkeypatch):
    monkeypatch.setattr(jobs, "_redis_conn", None)
    monkeypatch.setattr(jobs, "_redis_conn_checked", True)
    assert shared_cache.set("x", "k", {"a": 1}, 60) is False
    assert shared_cache.get("x", "k") is None


def test_analise_cacheada_vale_entre_instancias(fake_redis_conn):
    result = {"success": True, "analysis": "laudo", "usage": [{"model": "m"}], "exam_type": "joelho"}
    analyzer._store_cached_analysis("hash-1", result)
    analyzer._analysis_result_cache.clear()  # outra instância: memória vazia

    cached = analyzer._get_cached_analysis("hash-1")
    assert cached["analysis"] == "laudo" and cached["cache_hit"] is True and cached["usage"] == []
    assert fake_redis_conn.ttl("threehealth:cache:analise:hash-1") > 0


def test_valor_nao_serializavel_fica_fora(fake_redis_conn):
    assert shared_cache.set("x", "k", {"a": object()}, 60) is False


def test_pubmed_usa_cache_compartilhado(fake_redis_conn, monkeypatch):
    get = MagicMock(return_value={"esearchresult": {"idlist": ["1", "2"]}})
    monkeypatch.setattr(pubmed, "_get", get)
    assert pubmed._esearch("lca", deadline=0) == ["1", "2"]
    pubmed._cache.clear()
    assert pubmed._esearch("lca", deadline=0) == ["1", "2"]
    assert get.call_count == 1


# ── Monitor de erros de IA ────────────────────────────────────────────────────

def test_alerta_dispara_uma_vez_ao_passar_do_limite(monkeypatch, caplog):
    monkeypatch.setenv("AI_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("AI_ALERT_EMAIL", "equipe@example.com")
    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    send = MagicMock(return_value=True)
    monkeypatch.setattr(mailer, "send_email", send)

    for _ in range(5):
        ai_monitor.registrar("extracao_vazia", "laboratorial: Nenhum resultado")

    assert send.call_count == 1
    assert send.call_args.args[0] == "equipe@example.com"
    assert any(r.levelname == "ERROR" and "Alerta de IA" in r.message for r in caplog.records)
    resumo = {r["tipo"]: r for r in ai_monitor.resumo_do_dia()}
    assert resumo["extracao_vazia"]["total"] == 5 and resumo["extracao_vazia"]["alertado"]
    assert resumo["lenta"]["total"] == 0


def test_tipo_desconhecido_e_duracao(monkeypatch):
    monkeypatch.setenv("AI_SLOW_ANALYSIS_SECONDS", "60")
    ai_monitor.registrar("qualquer")
    ai_monitor.registrar_duracao(30)
    ai_monitor.registrar_duracao(75)
    assert cost_tracking.get_daily_counter("obs:falha_ia:lenta") == 1


def test_monitor_nunca_derruba_a_analise(monkeypatch):
    monkeypatch.setattr(cost_tracking, "increment_daily_counter", MagicMock(side_effect=RuntimeError("redis caiu")))
    ai_monitor.registrar("erro_provedor")  # não levanta


def test_erro_do_provedor_na_rota_e_contado():
    from web import presentation

    presentation.ai_error_response(RuntimeError("boom"))
    presentation.ai_error_response(analyzer.ExamReadError("não é exame"))
    assert cost_tracking.get_daily_counter("obs:falha_ia:erro_provedor") == 1


def test_painel_admin_mostra_falhas(client):
    ai_monitor.registrar("json_invalido", "x")
    from web import config

    client.get(f"/admin/entrar?key={config.get_admin_key()}")
    html = client.get("/admin/custos").get_data(as_text=True)
    assert "Falhas de IA hoje" in html and "resposta da IA sem JSON válido" in html


# ── Fila ──────────────────────────────────────────────────────────────────────

def test_plano_pago_vai_para_a_fila_prioritaria_com_retentativa(fake_redis_conn):
    job_premium = jobs.enqueue_analysis(owner="a", mode="premium")
    job_trial = jobs.enqueue_analysis(owner="b", mode="trial")
    alta = rq.Queue(jobs._PRIORITY_QUEUE_NAME, connection=fake_redis_conn)
    normal = rq.Queue(jobs._QUEUE_NAME, connection=fake_redis_conn)
    assert job_premium in alta.job_ids and job_trial in normal.job_ids
    assert rq.job.Job.fetch(job_premium, connection=fake_redis_conn).retries_left == 1
    assert [q.name for q in jobs.get_queues()] == [jobs._PRIORITY_QUEUE_NAME, jobs._QUEUE_NAME]


def test_reprocessar_falhas_ignora_erro_do_arquivo(fake_redis_conn, monkeypatch):
    monkeypatch.setenv("ANALYSIS_JOB_RETRIES", "0")
    queue = rq.Queue(jobs._QUEUE_NAME, connection=fake_redis_conn)
    falha_ia = queue.enqueue("core.jobs.nao_existe")
    falha_arquivo = queue.enqueue("core.jobs.nao_existe", meta={"user_error": "não é exame"})
    rq.worker.SimpleWorker([queue], connection=fake_redis_conn).work(burst=True)
    assert set(rq.registry.FailedJobRegistry(queue=queue).get_job_ids()) == {falha_ia.id, falha_arquivo.id}

    stats = jobs.requeue_failed()
    assert stats == {"reprocessados": 1, "ignorados": 1, "disponivel": True}
    assert falha_ia.id in queue.job_ids and falha_arquivo.id not in queue.job_ids


def test_arquivo_invalido_nao_e_retentado(fake_redis_conn, monkeypatch):
    chamadas = []

    def _analyze(**kwargs):
        chamadas.append(1)
        raise analyzer.ExamReadError("O arquivo não parece um exame.")

    monkeypatch.setattr(jobs, "_analyze", _analyze)
    job_id = jobs.enqueue_analysis(
        owner="a", mode="premium", images=[], image_meta=[], exam_filename="x.png", user_description="",
        model_name="m", api_key="k", anthropic_api_key="", exclude_gemini_cost=False, cost_scopes=[],
        persist={}, display_extra={},
    )
    queues = [rq.Queue(n, connection=fake_redis_conn) for n in (jobs._PRIORITY_QUEUE_NAME, jobs._QUEUE_NAME)]
    rq.worker.SimpleWorker(queues, connection=fake_redis_conn).work(burst=True)
    assert len(chamadas) == 1
    assert jobs.get_job_status(job_id)["error"] == "O arquivo não parece um exame."
