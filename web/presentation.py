"""Formatação para templates, leitura de formulários e mensagens ao usuário."""

from datetime import date, datetime

from flask import request

from core.analyzer import pretty_model_name
from web import config, security

_EXAM_LABELS = {
    "joelho": "Joelho",
    "coluna": "Coluna Vertebral",
    "ombro": "Ombro",
    "quadril": "Quadril",
    "pe_tornozelo": "Pé e Tornozelo",
    "mao_punho": "Mão e Punho",
    "cotovelo": "Cotovelo",
    "geral": "Região Ortopédica",
}


# ── Formulários ───────────────────────────────────────────────────────────────

def has_privacy_consent() -> bool:
    value = (request.form.get("privacy_consent") or "").strip().lower()
    return value in {"accepted", "on", "true", "1", "yes"}


def form_text(name: str, max_length: int = 120) -> str:
    value = (request.form.get(name) or "").strip()
    return " ".join(value.split())[:max_length]


def responsible_info_from_form() -> dict[str, str]:
    return {
        "name": form_text("responsible_name"),
        "role": form_text("responsible_role", 80),
        "register": form_text("responsible_register", 80),
        "organization": form_text("responsible_organization", 120),
    }


def responsible_info_from_row(row: dict | None) -> dict[str, str]:
    row = row or {}
    return {
        "name": row.get("responsavel_nome") or "",
        "role": row.get("responsavel_perfil") or "",
        "register": row.get("responsavel_registro") or "",
        "organization": row.get("responsavel_instituicao") or "",
    }


def split_validation_lines(value: str, limit: int = 12) -> list[str]:
    lines = []
    for line in (value or "").replace(";", "\n").splitlines():
        cleaned = " ".join(line.strip().split())
        if cleaned:
            lines.append(cleaned[:400])
        if len(lines) >= limit:
            break
    return lines


def score_value(value, default: int = 100) -> int:
    try:
        score = int(value)
    except Exception:
        score = default
    return max(0, min(score, 100))


def feedback_source_for_role(role: str = "") -> str:
    role_lower = (role or "").lower()
    if "radiologista" in role_lower or "laudista" in role_lower:
        return "radiologista"
    if security.is_admin(request):
        return "admin"
    return "medico"


def detect_modalidade(user_description: str) -> str | None:
    """Detecta a modalidade de imagem pela descrição do usuário."""
    desc = (user_description or "").lower()
    if any(k in desc for k in ["ressonância", "ressonancia", " rm ", "rm:", "mri", "ressonância magnética"]):
        return "RM"
    if any(k in desc for k in ["raio-x", "raio x", "radiografia", " rx ", "rx:", "x-ray"]):
        return "RX"
    if any(k in desc for k in ["tomografia", " tc ", "tc:", "ct scan"]):
        return "TC"
    if any(k in desc for k in ["ultrassom", "ultrassonografia", " us ", "us:", "ultrasound"]):
        return "US"
    return None


# ── Filtros de template ───────────────────────────────────────────────────────

def format_exam_label(value: str | None) -> str:
    key = (value or "").strip().lower()
    return _EXAM_LABELS.get(key, (value or "Exame Ortopédico").replace("_", " ").title())


def format_datetime_br(value) -> str:
    if not value:
        return "Data não informada"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if getattr(dt, "tzinfo", None) and config.LOCAL_TZ:
            dt = dt.astimezone(config.LOCAL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def format_date_br(value) -> str:
    """Data (date ou "AAAA-MM-DD…") como DD/MM/AAAA."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value.strftime("%d/%m/%Y")


# ── Resultado da análise ──────────────────────────────────────────────────────

def build_consensus_info(model_used: str = "", analysis: str = "") -> dict:
    model_text = model_used or ""
    lower_model = model_text.lower()
    dual = "claude" in lower_model and "consenso" in lower_model
    strong_count = (analysis or "").count("[FORTE]")
    moderate_count = (analysis or "").count("[MODERADO]")

    if dual:
        # model_used = "<gemini> + <claude> → consenso" (ver analyzer._produce_final_analysis)
        model_names = model_text.split("→")[0].split("+")
        model_a = pretty_model_name(model_names[0]) if model_names else "Gemini"
        model_b = pretty_model_name(model_names[1]) if len(model_names) > 1 else "Claude"
        return {
            "enabled": True,
            "status": "Consenso dual concluído",
            "label": "Gemini + Claude",
            "description": (
                "Dois modelos analisaram o exame de forma independente. A síntese final prioriza achados "
                "convergentes e rebaixa ou descarta achados com evidência insuficiente."
            ),
            "model_a": model_a,
            "model_b": model_b,
            "strong_count": strong_count,
            "moderate_count": moderate_count,
        }

    return {
        "enabled": False,
        "status": "Análise por IA concluída",
        "label": model_text or "Modelo principal",
        "description": (
            "Laudo gerado pelo modelo principal configurado no servidor. A revisão clínica do profissional "
            "responsável continua obrigatória."
        ),
        "model_a": model_text or "Modelo principal",
        "model_b": "",
        "strong_count": strong_count,
        "moderate_count": moderate_count,
    }


def result_view(result: dict) -> dict:
    """Campos comuns de um resultado de análise para a resposta (JSON ou template)."""
    return {
        "analysis": result["analysis"],
        "exam_type": result["exam_type"].replace("_", " ").title(),
        "references_used": result["references_used"],
        "model_used": result["model_used"],
        "consensus": build_consensus_info(result["model_used"], result["analysis"]),
    }


# ── Mensagens de erro da IA ───────────────────────────────────────────────────

def gemini_key_missing_message() -> str:
    if config.is_local_runtime():
        return (
            "Erro: chave do Gemini não configurada. No ambiente local, edite o arquivo .env "
            "e preencha GEMINI_API_KEY=sua_chave. Também aceito GOOGLE_API_KEY como alias."
        )
    return "Serviço de análise temporariamente indisponível. Tente novamente mais tarde."


def ai_error_response(exc: Exception) -> tuple[str, int]:
    """Mensagem para o usuário e status HTTP para uma falha da análise por IA.

    Antes cada rota tinha a própria cadeia de ifs (com textos diferentes para o
    mesmo erro). Detalhes de configuração só aparecem em ambiente local.
    """
    error_lower = str(exc).lower()
    code = getattr(exc, "code", None)
    if "api key not valid" in error_lower or "invalid api key" in error_lower or "api_key_invalid" in error_lower:
        if config.is_local_runtime():
            return "Erro de autenticação: chave do Gemini inválida. Verifique GEMINI_API_KEY no arquivo .env local.", 503
        return "Serviço de análise temporariamente indisponível. A equipe já foi notificada.", 503
    if code == 403 or "permission_denied" in error_lower:
        return "Erro de permissão na API de IA. A equipe já foi notificada.", 503
    if "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
        return "Limite de requisições atingido. Aguarde alguns instantes e tente novamente.", 429
    if code == 503 or "unavailable" in error_lower or "overloaded" in error_lower:
        return "O serviço de IA está temporariamente sobrecarregado. Aguarde alguns segundos e tente novamente.", 503
    if code == 404 or "not found" in error_lower:
        return "Modelo de IA temporariamente indisponível. Tente novamente em instantes.", 503
    return "Não foi possível processar o exame. Tente novamente.", 500
