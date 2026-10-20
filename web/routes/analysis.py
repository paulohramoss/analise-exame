"""
Análise de exames: página inicial, premium (/analyze), trial e API REST.

As três portas de entrada seguem o mesmo fluxo (_AnalysisInput → fila ou
execução síncrona → persistência):
- com Redis configurado (jobs.is_async_enabled()), a análise é enfileirada e a
  rota responde em milissegundos com um job_id; o worker (worker.py) processa;
- sem Redis, roda dentro da própria requisição (até ~90s).
"""

import hmac
import logging
import os
import time
from dataclasses import dataclass, field

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from core import analyzer, cost_tracking, db, jobs, privacy
from core.build_info import get_build_version
from web import config, costs, posthog, presentation, security, uploads
from web.extensions import rate_limit

logger = logging.getLogger(__name__)

bp = Blueprint("analysis", __name__)

BUILD_VERSION = get_build_version()


# ── Fluxo comum ───────────────────────────────────────────────────────────────

@dataclass
class _AnalysisInput:
    """Uploads já lidos para memória (arquivos temporários removidos)."""
    images: list[tuple[bytes, str]]
    image_meta: list[dict]
    exam_filename: str
    user_description: str
    analysis_description: str  # descrição + contexto extraído do DICOM
    display_images: list[dict] = field(default_factory=list)

    @classmethod
    def from_uploads(cls, upload_items: list[dict], user_description: str) -> "_AnalysisInput":
        # Identificadores do paciente (nome, CPF, telefone…) saem antes de o texto
        # ir para os provedores de IA ou para o banco: o laudo não precisa deles.
        user_description, removed = privacy.redact_identifiers(user_description)
        if removed:
            logger.info("Identificadores removidos da descrição clínica: %s", ", ".join(removed))
        images, image_meta = uploads.read_upload_bytes(upload_items)
        prepared = cls(
            images=images,
            image_meta=image_meta,
            exam_filename=upload_items[0].get("original_name") or "exame.jpg",
            user_description=user_description,
            analysis_description=uploads.description_with_dicom_context(user_description, upload_items),
            display_images=[
                {"b64": item["display_b64"], "mime": item["display_mime"], "source": item["source"]}
                for item in upload_items
            ],
        )
        uploads.cleanup_upload_files(upload_items)  # os bytes já estão em memória
        return prepared

    def persist_fields(self, **extra) -> dict:
        """Campos de db.salvar_analise comuns às três rotas."""
        return {
            "descricao_usuario": self.user_description,
            "modalidade": presentation.detect_modalidade(self.analysis_description),
            "ip_address": security.get_ip(),
            "user_agent": request.headers.get("User-Agent"),
            **extra,
        }


def _save_uploads(files) -> list[dict]:
    return [item for item in (uploads.save_upload_file(f) for f in files) if item is not None]


def _job_owner(mode: str) -> str | None:
    """Quem pode consultar o job. Trial não tem login: o job_id (UUID4) é o segredo."""
    if mode == "premium":
        if g.get("is_admin"):
            return "admin"
        return f"premium:{g.get('user_email') or 'local'}"
    if mode == "api":
        return costs.budget_user_key("api")
    return None


def _job_visible(status: dict, mode: str) -> bool:
    """O job pertence a quem está consultando? Admin enxerga os jobs premium de todos."""
    if status.get("status") == "not_found":
        return True  # nada a esconder; a rota já responde "não encontrado"
    if mode == "premium" and g.get("is_admin"):
        return True
    owner = status.get("owner")
    return owner is not None and hmac.compare_digest(owner, _job_owner(mode) or "")


def _public_status(status: dict) -> dict:
    return {key: value for key, value in status.items() if key != "owner"}


def _enqueue(mode: str, prepared: _AnalysisInput, persist: dict, display_extra: dict | None = None) -> str | None:
    """Enfileira a análise. Retorna o job_id, ou None se a fila não está disponível.

    As chaves de API não vão no job: o worker lê as próprias do ambiente.
    """
    if not jobs.is_async_enabled():
        return None
    return jobs.enqueue_analysis(
        owner=_job_owner(mode),
        mode=mode,
        images=prepared.images,
        image_meta=prepared.image_meta,
        exam_filename=prepared.exam_filename,
        user_description=prepared.analysis_description,
        model_name=config.get_model_name(),
        cost_scopes=costs.cost_budget_scopes(mode),
        persist=persist,
        display_extra=display_extra,
    )


def _run_now(mode: str, prepared: _AnalysisInput, api_key: str, persist: dict) -> dict:
    """Executa a análise na própria requisição, registra o custo e persiste. Retorna o resultado."""
    t0 = time.time()
    result = analyzer.analyze_exam_from_bytes(
        exam_images_data=prepared.images,
        exam_filename=prepared.exam_filename,
        api_key=api_key,
        user_description=prepared.analysis_description,
        model_name=config.get_model_name(),
        anthropic_api_key=config.get_anthropic_api_key(),
        posthog_client=posthog.init_posthog(),
        posthog_distinct_id=str(g.cliente_id) if g.get("cliente_id") else None,
        ai_trace_id=g.get("trace_id"),
        on_late_usage=cost_tracking.late_usage_recorder(costs.cost_budget_scopes(mode)),
    )
    costs.record_analysis_cost(mode, result)
    result["analise_id"] = db.salvar_analise_com_imagens(
        result, mode, prepared.images, prepared.image_meta,
        tempo_ms=int((time.time() - t0) * 1000), **persist,
    )
    g.analise_id = result["analise_id"]
    return result


# ── Health check e página inicial ─────────────────────────────────────────────

@bp.route("/health")
def health():
    """
    Verifica conectividade com as dependências externas (Redis da fila
    assíncrona e Supabase). Usado por monitoramento externo do worker
    (Railway/Render), que roda como processo separado sem endpoint próprio.
    Cada dependência é "disabled" se não configurada, "ok"/"down" se configurada.
    """
    checks = {}
    degraded = False

    if jobs.is_async_enabled():
        redis_ok = jobs.health_check_redis()
        checks["redis"] = "ok" if redis_ok else "down"
        degraded = degraded or not redis_ok
    else:
        checks["redis"] = "disabled"

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        supabase_ok = db.health_check()
        checks["supabase"] = "ok" if supabase_ok else "down"
        degraded = degraded or not supabase_ok
    else:
        checks["supabase"] = "disabled"

    body = {"status": "degraded" if degraded else "ok", "build_version": BUILD_VERSION, "checks": checks}
    return jsonify(body), 503 if degraded else 200


@bp.route("/")
def index():
    return render_template(
        "index.html",
        premium=security.is_premium(request),
        max_images=uploads.max_images_for_request(),
    )


# ── Premium ───────────────────────────────────────────────────────────────────

@bp.route("/analyze", methods=["POST"])
@security.premium_required
def analyze():
    """Recebe e analisa o(s) exame(s) médico(s) do usuário premium."""
    g.modo = "premium"

    def _back(message: str):
        flash(message, "error")
        return redirect(url_for("analysis.index"))

    if not presentation.has_privacy_consent():
        return _back("Confirme o consentimento LGPD e a ciência de que a IA é ferramenta de apoio antes de enviar o exame.")

    # Dados do responsável são opcionais: quando vazios, laudo e PDF caem no
    # texto neutro ("Responsável não informado") já previsto nos templates.
    responsible_info = presentation.responsible_info_from_form()

    api_key = config.get_api_key()
    if not config.has_ai_provider():
        return _back(presentation.gemini_key_missing_message())
    if costs.blocked_by_cost_budget("premium"):
        return _back("Limite de gasto diário atingido. Tente novamente mais tarde ou contate o suporte.")

    files = uploads.files_from_request()[:uploads.max_images_for_request()]
    if not files:
        return _back("Nenhuma imagem enviada.")
    upload_items = _save_uploads(files)
    if not upload_items:
        return _back(f"Formato de arquivo não suportado. Use: {', '.join(sorted(uploads.ALLOWED_EXTENSIONS))}")

    prepared = _AnalysisInput.from_uploads(upload_items, request.form.get("description", "").strip())
    persist = prepared.persist_fields(cliente_id=g.get("cliente_id"), responsavel=responsible_info)

    job_id = _enqueue("premium", prepared, persist, display_extra={
        "images_data": prepared.display_images,
        "user_description_display": prepared.user_description,
        "responsible": responsible_info,
    })
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture(
            "premium_analysis_submitted",
            properties={"image_count": len(prepared.images), "processing_mode": "async" if job_id else "sync"},
        )
    if job_id:
        return redirect(url_for("analysis.analyze_processando", job_id=job_id))

    try:
        result = _run_now("premium", prepared, api_key, persist)
    except Exception as e:
        logger.exception("Falha na análise de exame (%s)", type(e).__name__)
        return _back(presentation.ai_error_response(e)[0])

    return render_template(
        "result.html",
        **presentation.result_view(result),
        images=prepared.display_images,
        num_images=result["num_images"],
        user_description=prepared.user_description,
        responsible=responsible_info,
        analysis_id=result["analise_id"],
    )


@bp.route("/analyze/processando/<job_id>")
@security.premium_required
def analyze_processando(job_id: str):
    """Página de espera (poll) enquanto a análise premium é processada de forma assíncrona."""
    return render_template(
        "processando.html",
        status_url=url_for("analysis.analyze_status", job_id=job_id),
        resultado_url=url_for("analysis.analyze_resultado", job_id=job_id),
    )


@bp.route("/analyze/status/<job_id>")
@security.premium_required
@rate_limit("STATUS_POLL_RATE_LIMIT", "60 per minute")
def analyze_status(job_id: str):
    """Consulta o status de uma análise premium enfileirada (usado pela página de espera)."""
    status = jobs.get_job_status(job_id)
    if not _job_visible(status, "premium"):
        return jsonify({"status": "not_found", "error": ""}), 404
    return jsonify({"status": status["status"], "stage": status.get("stage"), "error": status.get("error", "")})


@bp.route("/analyze/resultado/<job_id>")
@security.premium_required
def analyze_resultado(job_id: str):
    """Renderiza o resultado de uma análise premium assíncrona já concluída."""
    status = jobs.get_job_status(job_id)
    if status["status"] == "not_found" or not _job_visible(status, "premium"):
        flash("Análise não encontrada para este acesso.", "error")
        return redirect(url_for("analysis.index"))
    if status["status"] != "finished":
        return redirect(url_for("analysis.analyze_processando", job_id=job_id))

    result = status["result"]
    return render_template(
        "result.html",
        **presentation.result_view(result),
        images=result.get("images_data") or [],
        num_images=result["num_images"],
        user_description=result.get("user_description_display", ""),
        responsible=result.get("responsible") or {"name": "", "role": ""},
        analysis_id=result.get("analise_id"),
    )


# ── Trial (gratuito) ──────────────────────────────────────────────────────────

@bp.route("/trial")
def trial():
    """Página de teste gratuito, embutida via iframe na landing page (WordPress)."""
    return render_template("trial.html")


@bp.route("/trial/analyze", methods=["POST"])
@rate_limit("TRIAL_RATE_LIMIT", "10 per hour")
def trial_analyze():
    """Análise no modo de teste gratuito (retorna JSON, aceita apenas 1 imagem)."""
    g.modo = "trial"

    if not presentation.has_privacy_consent():
        return jsonify({"error": "Confirme o consentimento LGPD e a ciência de que a IA é ferramenta de apoio."}), 400

    api_key = config.get_api_key()
    if not config.has_ai_provider():
        return jsonify({"error": "Serviço temporariamente indisponível. Tente novamente mais tarde."}), 503

    if costs.blocked_by_cost_budget("trial"):
        return jsonify({"error": "Limite de uso gratuito diário atingido. Tente novamente mais tarde."}), 429

    trial_limit = costs.trial_limit_per_ip()
    trial_scope = costs.trial_quota_scope()
    if trial_limit and cost_tracking.get_daily_counter(trial_scope) >= trial_limit:
        return jsonify({
            "error": "Você já usou todas as análises gratuitas de hoje. Contrate um plano para continuar.",
            "trial_exhausted": True,
            "trial_remaining": 0,
        }), 429

    file = request.files.get("exam_image")
    if file is None:
        return jsonify({"error": "Nenhuma imagem enviada."}), 400
    if file.filename == "":
        return jsonify({"error": "Nenhum arquivo selecionado."}), 400
    if not uploads.allowed_file(file.filename):
        return jsonify({"error": f"Formato não suportado. Use: {', '.join(sorted(uploads.ALLOWED_EXTENSIONS))}"}), 400

    upload_items = _save_uploads([file])
    if not upload_items:
        return jsonify({"error": "Erro ao processar o arquivo enviado."}), 400

    # Conta a análise ao aceitá-la (não ao terminar): pedidos em paralelo não
    # furam a cota, e uma análise que falha por erro nosso custa só uma tentativa.
    trial_remaining = None
    if trial_limit:
        trial_remaining = max(0, trial_limit - int(cost_tracking.increment_daily_counter(trial_scope)))

    prepared = _AnalysisInput.from_uploads(upload_items, request.form.get("description", "").strip())
    display = prepared.display_images[0]
    persist = prepared.persist_fields()

    job_id = _enqueue("trial", prepared, persist, display_extra={"image_b64": display["b64"], "image_mime": display["mime"]})
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture(
            "trial_analysis_submitted",
            properties={"processing_mode": "async" if job_id else "sync"},
        )
    if job_id:
        return jsonify({
            "status": "queued",
            "job_id": job_id,
            "status_url": url_for("analysis.trial_analyze_status", job_id=job_id),
            "trial_remaining": trial_remaining,
        }), 202

    try:
        result = _run_now("trial", prepared, api_key, persist)
    except Exception as e:
        logger.exception("Falha na análise trial (%s)", type(e).__name__)
        message, status_code = presentation.ai_error_response(e)
        return jsonify({"error": message}), status_code

    return jsonify({
        **presentation.result_view(result),
        "image_b64": display["b64"],
        "image_mime": display["mime"],
        "trial_remaining": trial_remaining,
    }), 200


@bp.route("/trial/analyze/status/<job_id>")
@rate_limit("STATUS_POLL_RATE_LIMIT", "60 per minute")
def trial_analyze_status(job_id: str):
    """Consulta o status de uma análise trial enfileirada."""
    status = jobs.get_job_status(job_id)
    if status["status"] == "finished":
        result = status["result"]
        return jsonify({
            "status": "finished",
            **presentation.result_view(result),
            "image_b64": result.get("image_b64", ""),
            "image_mime": result.get("image_mime", ""),
        }), 200
    if status["status"] == "not_found":
        return jsonify({"status": "not_found", "error": "Job não encontrado."}), 404
    return jsonify(_public_status(status)), 200


# ── API REST ──────────────────────────────────────────────────────────────────

_API_KEY_ERROR = "Chave de acesso à API ausente ou inválida (cabeçalho X-API-Key)."


def _api_access_key_valid() -> bool:
    """X-API-Key precisa ser uma das chaves em API_ACCESS_KEYS (CSV).

    Sem chaves configuradas a API fica desligada. Antes, uma requisição sem
    chave caía na GEMINI_API_KEY do servidor (e a dupla análise usava sempre
    nossa chave Anthropic), permitindo uso anônimo e ilimitado às nossas custas.
    """
    supplied = request.headers.get("X-API-Key", "").strip()
    if not supplied:
        return False
    valid_keys = [k.strip() for k in os.environ.get("API_ACCESS_KEYS", "").split(",") if k.strip()]
    return any(hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8")) for key in valid_keys)


@bp.route("/api/analyze", methods=["POST"])
@rate_limit("API_RATE_LIMIT", "60 per hour")
def api_analyze():
    """Endpoint REST para integração programática. Suporta múltiplas imagens."""
    g.modo = "api"

    if not _api_access_key_valid():
        return jsonify({"error": _API_KEY_ERROR}), 401

    api_key = config.get_api_key()
    if not config.has_ai_provider():
        return jsonify({"error": "Serviço temporariamente indisponível."}), 503

    if costs.blocked_by_cost_budget("api"):
        return jsonify({"error": "Limite de gasto diário atingido para esta chave. Tente novamente mais tarde."}), 429

    files = uploads.files_from_request()
    if not files:
        return jsonify({"error": "Nenhuma imagem enviada"}), 400
    files = [f for f in files if uploads.allowed_file(f.filename)][:uploads.max_images_for_request()]
    if not files:
        return jsonify({"error": "Formato de arquivo não suportado"}), 400
    upload_items = _save_uploads(files)
    if not upload_items:
        return jsonify({"error": "Nenhum arquivo válido pôde ser processado"}), 400

    prepared = _AnalysisInput.from_uploads(upload_items, request.form.get("description", ""))
    persist = prepared.persist_fields()

    job_id = _enqueue("api", prepared, persist)
    if job_id:
        return jsonify({
            "success": True,
            "status": "queued",
            "job_id": job_id,
            "status_url": url_for("analysis.api_analyze_status", job_id=job_id),
        }), 202

    try:
        result = _run_now("api", prepared, api_key, persist)
    except Exception as e:
        logger.exception("Falha na análise via API (%s)", type(e).__name__)
        message, status_code = presentation.ai_error_response(e)
        return jsonify({"error": message, "success": False}), status_code

    result["consensus"] = presentation.build_consensus_info(result.get("model_used", ""), result.get("analysis", ""))
    return jsonify(result), 200


@bp.route("/api/analyze/status/<job_id>")
@rate_limit("STATUS_POLL_RATE_LIMIT", "60 per minute")
def api_analyze_status(job_id: str):
    """Consulta o status de uma análise enfileirada via /api/analyze."""
    if not _api_access_key_valid():
        return jsonify({"error": _API_KEY_ERROR}), 401
    status = jobs.get_job_status(job_id)
    if not _job_visible(status, "api"):
        status = {"status": "not_found"}
    if status["status"] == "finished":
        result = dict(status["result"])
        result["consensus"] = presentation.build_consensus_info(result.get("model_used", ""), result.get("analysis", ""))
        return jsonify({"status": "finished", **result}), 200
    if status["status"] == "not_found":
        return jsonify({"status": "not_found", "error": "Job não encontrado."}), 404
    return jsonify(_public_status(status)), 200
