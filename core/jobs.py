"""
Fila de análise assíncrona (RQ + Redis).

Por quê: a análise (Gemini + Claude) pode levar até ~90s. Rodá-la dentro do
ciclo de request/response do Flask significa que um worker HTTP fica
ocupado por até 90s por análise — sob carga, isso esgota rapidamente a
capacidade de atender outras requisições (mesmo as rápidas, como servir a
página inicial). Enfileirando o trabalho, o endpoint responde em
milissegundos com um job_id e o cliente faz polling do status; quem
efetivamente processa é um worker RQ separado (`worker.py`), que pode rodar
em outra máquina/serviço e escalar independentemente do processo web.

Este módulo não depende de Flask: `run_analysis_job` roda no processo do
worker, que não tem `request`/`g`. Tudo que a rota precisa (escopos de
orçamento, flag de custo BYOK) é resolvido no processo web (onde há
contexto de request) e passado como dado simples para o job.

Requer Redis. Se REDIS não estiver configurado (nem em RATELIMIT_STORAGE_URI
nem em ANALYSIS_QUEUE_REDIS_URL), `is_async_enabled()` retorna False e as
rotas devem usar o caminho síncrono existente como fallback.
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

_QUEUE_NAME = os.environ.get("ANALYSIS_QUEUE_NAME", "analysis")
_JOB_TIMEOUT_SECONDS = int(os.environ.get("ANALYSIS_JOB_TIMEOUT_SECONDS", "180"))
_JOB_RESULT_TTL_SECONDS = int(os.environ.get("ANALYSIS_JOB_RESULT_TTL_SECONDS", "3600"))

_redis_conn = None
_redis_conn_checked = False


def _queue_redis_url() -> str:
    return (
        os.environ.get("ANALYSIS_QUEUE_REDIS_URL", "").strip()
        or os.environ.get("RATELIMIT_STORAGE_URI", "").strip()
    )


def is_async_enabled() -> bool:
    """True se há um Redis configurado para a fila (ver _queue_redis_url)."""
    return _queue_redis_url().startswith(("redis://", "rediss://"))


def _get_redis_connection():
    global _redis_conn, _redis_conn_checked
    if _redis_conn_checked:
        return _redis_conn
    _redis_conn_checked = True
    if not is_async_enabled():
        return None
    try:
        import redis
        _redis_conn = redis.from_url(_queue_redis_url())
        _redis_conn.ping()
    except Exception:
        logger.warning("Não foi possível conectar ao Redis da fila de análise.", exc_info=True)
        _redis_conn = None
    return _redis_conn


def get_queue():
    """Retorna a Queue RQ, ou None se Redis não estiver disponível."""
    connection = _get_redis_connection()
    if connection is None:
        return None
    import rq
    return rq.Queue(_QUEUE_NAME, connection=connection)


def health_check_redis() -> bool:
    """
    True se a fila assíncrona está configurada e o Redis responde a um ping
    agora. Usa uma conexão própria e de vida curta (em vez do singleton
    cacheado em _get_redis_connection) para não reportar "down" indefinidamente
    caso o Redis estivesse fora do ar só no boot do processo.
    """
    url = _queue_redis_url()
    if not url.startswith(("redis://", "rediss://")):
        return False
    try:
        import redis
        redis.from_url(url, socket_connect_timeout=2, socket_timeout=2).ping()
        return True
    except Exception:
        return False


def enqueue_analysis(**kwargs) -> str | None:
    """Enfileira uma análise. Retorna o job_id, ou None se a fila não está disponível."""
    queue = get_queue()
    if queue is None:
        return None
    job = queue.enqueue(
        "core.jobs.run_analysis_job",
        kwargs=kwargs,
        job_timeout=_JOB_TIMEOUT_SECONDS,
        result_ttl=_JOB_RESULT_TTL_SECONDS,
        failure_ttl=_JOB_RESULT_TTL_SECONDS,
    )
    logger.info("Análise enfileirada (job_id=%s, mode=%s)", job.id, kwargs.get("mode"))
    return job.id


def get_job_status(job_id: str) -> dict:
    """
    Retorna {"status": "queued"|"started"|"finished"|"failed"|"not_found", ...}.
    Em "finished", inclui "result" (o dict retornado por run_analysis_job).
    Em "failed", inclui "error" com uma mensagem genérica (não expõe traceback interno).
    """
    connection = _get_redis_connection()
    if connection is None:
        return {"status": "not_found"}
    import rq
    try:
        job = rq.job.Job.fetch(job_id, connection=connection)
    except Exception:
        return {"status": "not_found"}

    status = job.get_status(refresh=True)
    payload = {"status": status}
    if status == "finished":
        payload["result"] = job.return_value()
    elif status == "failed":
        payload["error"] = "Não foi possível processar a análise. Tente novamente."
    return payload


def run_analysis_job(
    *,
    mode: str,
    images: list,
    image_meta: list,
    exam_filename: str,
    api_key: str,
    user_description: str,
    model_name: str,
    anthropic_api_key: str,
    exclude_gemini_cost: bool,
    cost_scopes: list,
    persist: dict,
    display_extra: dict | None = None,
) -> dict:
    """
    Executado pelo worker RQ (processo separado, sem contexto Flask).

    images: lista de (bytes, mime_type) — mesmo formato de analyze_exam_from_bytes.
    image_meta: lista alinhada por índice com `images`, cada item
        {"origem", "arquivo_original", "dicom_metadata"} para persistência.
    exclude_gemini_cost: True quando a chamada foi feita com X-API-Key do
        próprio chamador (modo "api") — nesse caso, só o custo do Claude
        (sempre pago por nós na síntese dual) entra no nosso orçamento.
    cost_scopes: [(scope, cap_usd), ...] pré-calculados no processo web.
    persist: kwargs extras para db.salvar_analise (ip_address, user_agent,
        cliente_id, responsavel, descricao_usuario, modalidade, sessao_id).
    display_extra: dados só de exibição (ex.: miniaturas em base64, nome do
        responsável) que a rota "resultado" precisa mas que o job não usa —
        são apenas devolvidos junto do resultado, sem processamento.
    """
    import hashlib
    from core import cost_tracking, db
    from core.analyzer import analyze_exam_from_bytes

    t0 = time.time()
    result = analyze_exam_from_bytes(
        exam_images_data=images,
        exam_filename=exam_filename,
        api_key=api_key,
        user_description=user_description,
        model_name=model_name,
        anthropic_api_key=anthropic_api_key,
    )

    usage = result.get("usage", [])
    cost_tracking.log_analysis_cost(result.get("exam_type", ""), result.get("model_used", ""), usage)
    own_usage = [item for item in usage if not (exclude_gemini_cost and item["model"].startswith("gemini"))]
    own_cost_usd = sum(
        cost_tracking.estimate_cost_usd(item["model"], item["input_tokens"], item["output_tokens"])
        for item in own_usage
    )
    if own_cost_usd > 0:
        for scope, _cap in cost_scopes:
            cost_tracking.add_spend(scope, own_cost_usd)

    analise_id = db.salvar_analise(
        tipo_exame=result["exam_type"],
        analise_completa=result["analysis"],
        modelo_ia=result["model_used"],
        referencias_usadas=result["references_used"],
        num_imagens=result["num_images"],
        modo=mode,
        tempo_ms=int((time.time() - t0) * 1000),
        **persist,
    )
    result["analise_id"] = analise_id

    if analise_id:
        for i, (raw_bytes, mime) in enumerate(images, 1):
            meta = image_meta[i - 1] if i - 1 < len(image_meta) else {}
            db.salvar_imagem_exame(
                analise_id=analise_id,
                mime_type=mime,
                tamanho_bytes=len(raw_bytes),
                hash_md5=hashlib.md5(raw_bytes).hexdigest(),
                ordem=i,
                origem=meta.get("origem") or "upload",
                arquivo_original=meta.get("arquivo_original") or "",
                dicom_metadata=meta.get("dicom_metadata") or None,
            )

    if display_extra:
        result.update(display_extra)
    return result
