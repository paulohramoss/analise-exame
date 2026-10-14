"""
Custo estimado por análise (tokens × preço) e orçamentos de gasto diário.

Não faz nenhuma chamada de rede: apenas calcula custo a partir de contagens de
tokens já retornadas pelas APIs do Gemini/Claude, loga o resultado de forma
estruturada, e mantém contadores de gasto diário por "escopo" (ex.: trial
global, um usuário específico) para permitir bloquear análises quando um
limite configurado é excedido.

Armazenamento dos contadores, nesta ordem:
1. Redis — RATELIMIT_STORAGE_URI ou ANALYSIS_QUEUE_REDIS_URL (mesma infra do
   flask-limiter e da fila);
2. Supabase — tabela gastos_diarios + função somar_gasto_diario (migration
   2026-10-13-gastos-diarios.sql), com incremento atômico no banco;
3. memória do processo — só para desenvolvimento local.

Antes só existiam Redis e memória: na Vercel sem Redis, cada instância
serverless tinha o próprio contador zerado a cada cold start, e o teto
"diário" do trial praticamente não existia.

Os mesmos contadores servem para contagens que não são dinheiro (ex.: análises
do trial por IP) — ver increment_daily_counter.
"""

import logging
import os
import threading
from datetime import datetime, timezone

from core import db

logger = logging.getLogger(__name__)

# Preços aproximados em USD por 1 milhão de tokens. Mudam com frequência —
# configuráveis via env para não exigir deploy a cada ajuste de tabela dos
# provedores. Os defaults abaixo são apenas uma referência aproximada.
#
# A busca é por prefixo de família (ordem importa: o mais específico primeiro),
# para que trocar CLAUDE_MODEL/GEMINI_MODEL por outra versão da mesma família
# continue sendo cobrado. Antes era por nome exato: um modelo novo custava
# US$ 0 e o orçamento diário deixava de funcionar sem nenhum aviso.
_PRICING_PER_1M_TOKENS = [
    ("gemini-2.5-flash-lite", (
        float(os.environ.get("GEMINI_2_5_FLASH_LITE_INPUT_PRICE_PER_1M", "0.10")),
        float(os.environ.get("GEMINI_2_5_FLASH_LITE_OUTPUT_PRICE_PER_1M", "0.40")),
    )),
    ("gemini-2.5-flash", (
        float(os.environ.get("GEMINI_2_5_FLASH_INPUT_PRICE_PER_1M", "0.30")),
        float(os.environ.get("GEMINI_2_5_FLASH_OUTPUT_PRICE_PER_1M", "2.50")),
    )),
    ("gemini-2.5-pro", (
        float(os.environ.get("GEMINI_2_5_PRO_INPUT_PRICE_PER_1M", "1.25")),
        float(os.environ.get("GEMINI_2_5_PRO_OUTPUT_PRICE_PER_1M", "10.00")),
    )),
    ("gemini-2.0-flash-lite", (
        float(os.environ.get("GEMINI_2_0_FLASH_LITE_INPUT_PRICE_PER_1M", "0.075")),
        float(os.environ.get("GEMINI_2_0_FLASH_LITE_OUTPUT_PRICE_PER_1M", "0.30")),
    )),
    ("gemini-2.0-flash", (
        float(os.environ.get("GEMINI_2_0_FLASH_INPUT_PRICE_PER_1M", "0.10")),
        float(os.environ.get("GEMINI_2_0_FLASH_OUTPUT_PRICE_PER_1M", "0.40")),
    )),
    ("claude-haiku", (
        float(os.environ.get("CLAUDE_HAIKU_INPUT_PRICE_PER_1M", "1.00")),
        float(os.environ.get("CLAUDE_HAIKU_OUTPUT_PRICE_PER_1M", "5.00")),
    )),
    ("claude-sonnet", (
        float(os.environ.get("CLAUDE_SONNET_INPUT_PRICE_PER_1M", "3.00")),
        float(os.environ.get("CLAUDE_SONNET_OUTPUT_PRICE_PER_1M", "15.00")),
    )),
    ("claude-opus", (
        float(os.environ.get("CLAUDE_OPUS_INPUT_PRICE_PER_1M", "5.00")),
        float(os.environ.get("CLAUDE_OPUS_OUTPUT_PRICE_PER_1M", "25.00")),
    )),
]
# Modelo desconhecido: cobra como o mais caro da família em uso, nunca como
# grátis — superestimar só aperta o orçamento; subestimar o desliga.
_UNKNOWN_FAMILY_PRICING = {
    "gemini": dict(_PRICING_PER_1M_TOKENS)["gemini-2.5-pro"],
    "claude": dict(_PRICING_PER_1M_TOKENS)["claude-opus"],
}
_warned_unknown_models: set[str] = set()


def _pricing_for(model: str) -> tuple[float, float]:
    name = (model or "").strip().lower()
    for prefix, pricing in _PRICING_PER_1M_TOKENS:
        if name.startswith(prefix):
            return pricing
    family = name.split("-", 1)[0]
    pricing = _UNKNOWN_FAMILY_PRICING.get(family, _UNKNOWN_FAMILY_PRICING["claude"])
    if name not in _warned_unknown_models:
        _warned_unknown_models.add(name)
        logger.warning(
            "Modelo sem preço configurado (%s); estimando custo pelo teto da família (%.2f/%.2f USD por 1M).",
            model, *pricing,
        )
    return pricing


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Custo estimado em USD para uma chamada com as contagens de tokens dadas."""
    input_price, output_price = _pricing_for(model)
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


def log_analysis_cost(exam_type: str, model_used: str, usage: list[dict]) -> float:
    """
    Loga o detalhamento de custo de uma análise (uma entrada por chamada de
    modelo: Gemini, Claude análise independente, Claude síntese) e retorna o
    custo total estimado em USD.

    `usage` é a lista retornada por analyze_exam()/analyze_exam_from_bytes()
    em result["usage"]: [{"model", "input_tokens", "output_tokens"}, ...].
    """
    breakdown = []
    total = 0.0
    for item in usage:
        cost = estimate_cost_usd(item["model"], item["input_tokens"], item["output_tokens"])
        total += cost
        breakdown.append({**item, "cost_usd": round(cost, 6)})

    logger.info(
        "Custo estimado da análise: $%.5f",
        total,
        extra={
            "exam_type": exam_type,
            "model_used": model_used,
            "cost_usd": round(total, 6),
            "usage": breakdown,
        },
    )
    return total


# ─────────────────────────────────────────────────────────────────────────────
# ORÇAMENTO DE GASTO DIÁRIO
# ─────────────────────────────────────────────────────────────────────────────

_memory_lock = threading.Lock()
_memory_spend: dict[str, tuple[str, float]] = {}  # scope -> (dia, total_usd)

_redis_client = None
_redis_checked = False


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _redis_uri() -> str:
    for name in ("RATELIMIT_STORAGE_URI", "ANALYSIS_QUEUE_REDIS_URL"):
        uri = os.environ.get(name, "").strip()
        if uri.startswith(("redis://", "rediss://")):
            return uri
    return ""


def _get_redis_client():
    """Reaproveita o Redis do flask-limiter ou da fila de análise, se houver."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    uri = _redis_uri()
    if not uri:
        return None
    try:
        import redis
        _redis_client = redis.from_url(uri)
        _redis_client.ping()
    except Exception:
        logger.warning("Não foi possível conectar ao Redis para orçamento de custo; usando memória local.", exc_info=True)
        _redis_client = None
    return _redis_client


# Contadores de orçamento só importam no dia; os de observabilidade (obs:*)
# alimentam o histórico de /admin/custos e duram mais no Redis.
_BUDGET_TTL_SECONDS = 2 * 24 * 3600
_OBSERVABILITY_TTL_SECONDS = 95 * 24 * 3600


def add_spend(scope: str, amount_usd: float) -> float:
    """Soma `amount_usd` ao gasto acumulado do dia (UTC) para `scope`. Retorna o total atualizado."""
    return _add_to_daily_counter(scope, amount_usd)


def _add_to_daily_counter(scope: str, amount_usd: float) -> float:
    if amount_usd <= 0:
        return get_spend(scope)

    redis_client = _get_redis_client()
    if redis_client is not None:
        key = f"cost_budget:{scope}:{_today()}"
        try:
            total = redis_client.incrbyfloat(key, amount_usd)
            redis_client.expire(
                key, _OBSERVABILITY_TTL_SECONDS if scope.startswith("obs:") else _BUDGET_TTL_SECONDS
            )
            return float(total)
        except Exception:
            logger.warning("Falha ao registrar gasto no Redis; tentando Supabase.", exc_info=True)

    total = db.somar_gasto_diario(scope, _today(), amount_usd)
    if total is not None:
        return total

    with _memory_lock:
        day, total = _memory_spend.get(scope, (_today(), 0.0))
        if day != _today():
            total = 0.0
        total += amount_usd
        _memory_spend[scope] = (_today(), total)
        return total


def get_spend(scope: str) -> float:
    """Gasto acumulado hoje (UTC) para `scope`."""
    return get_spend_on(scope, _today())


def get_spend_on(scope: str, day: str) -> float:
    """Total do contador `scope` no dia `day` (YYYY-MM-DD, UTC).

    Na memória do processo só existe o dia corrente; dias anteriores exigem
    Redis ou Supabase.
    """
    redis_client = _get_redis_client()
    if redis_client is not None:
        key = f"cost_budget:{scope}:{day}"
        try:
            value = redis_client.get(key)
            return float(value) if value is not None else 0.0
        except Exception:
            logger.warning("Falha ao consultar gasto no Redis; tentando Supabase.", exc_info=True)

    total = db.buscar_gasto_diario(scope, day)
    if total is not None:
        return total

    with _memory_lock:
        stored_day, total = _memory_spend.get(scope, (day, 0.0))
        return total if stored_day == day == _today() else 0.0


def is_budget_exceeded(scope: str, cap_usd: float) -> bool:
    """cap_usd <= 0 desativa o limite (sempre retorna False)."""
    if cap_usd <= 0:
        return False
    return get_spend(scope) >= cap_usd


def increment_daily_counter(scope: str, amount: float = 1.0) -> float:
    """Contador diário genérico (mesmo armazenamento do orçamento). Retorna o total do dia."""
    return _add_to_daily_counter(scope, amount)


def get_daily_counter(scope: str) -> float:
    return get_spend(scope)


def late_usage_recorder(scopes: list[tuple[str, float]]):
    """Callback para analyzer.analyze_exam_from_bytes(on_late_usage=...).

    Credita nos orçamentos o custo de uma chamada de IA que terminou depois do
    prazo e teve o resultado descartado — o provedor cobra mesmo assim. Roda em
    outra thread, sem contexto de request: os escopos vêm prontos.
    """
    def _record(item: dict) -> None:
        cost = estimate_cost_usd(item["model"], item["input_tokens"], item["output_tokens"])
        log_analysis_cost("chamada_descartada", item["model"], [item])
        if cost > 0:
            for scope, _cap in scopes:
                add_spend(scope, cost)

    return _record


# ─────────────────────────────────────────────────────────────────────────────
# OBSERVABILIDADE: análises, custo e degradação do consenso por dia e modo
# ─────────────────────────────────────────────────────────────────────────────

MODES = ("premium", "trial", "api")
_FALLBACK_MARKER = "consenso rápido"


def fallback_alert_settings() -> tuple[float, int]:
    try:
        ratio = float(os.environ.get("CONSENSUS_FALLBACK_ALERT_RATIO", "0.3"))
        minimum = int(os.environ.get("CONSENSUS_FALLBACK_ALERT_MIN_ANALYSES", "10"))
    except ValueError:
        ratio, minimum = 0.3, 10
    return ratio, minimum


def record_outcome(mode: str, model_used: str, cost_usd: float) -> None:
    """Registra uma análise concluída nos contadores diários de observabilidade.

    "Consenso rápido" = o laudo saiu de um só modelo porque o outro ou a síntese
    não coube no orçamento de tempo (ver analyzer._produce_final_analysis).
    Uma taxa alta disso significa laudos piores sem nenhum erro visível — por
    isso gera um alerta (logger.error → Sentry) uma vez por dia.
    """
    increment_daily_counter(f"obs:analises:{mode}")
    if cost_usd > 0:
        increment_daily_counter(f"obs:custo:{mode}", cost_usd)
    if _FALLBACK_MARKER in (model_used or ""):
        increment_daily_counter(f"obs:consenso_rapido:{mode}")
        _maybe_alert_fallback_rate()


def _maybe_alert_fallback_rate() -> None:
    ratio_limit, minimum = fallback_alert_settings()
    analyses = sum(get_daily_counter(f"obs:analises:{m}") for m in MODES)
    fallbacks = sum(get_daily_counter(f"obs:consenso_rapido:{m}") for m in MODES)
    if analyses < minimum or fallbacks / analyses < ratio_limit:
        return
    # O próprio contador deduplica entre instâncias: só quem o leva a 1 alerta.
    if increment_daily_counter("obs:alerta_consenso_rapido") == 1:
        logger.error(
            "Taxa de 'consenso rápido' alta hoje: %d de %d análises (%.0f%%, limite %.0f%%). "
            "Verifique timeouts, ANALYSIS_BUDGET_SECONDS e a saúde das APIs de IA.",
            int(fallbacks), int(analyses), 100 * fallbacks / analyses, 100 * ratio_limit,
        )


def has_persistent_storage() -> bool:
    """True se os contadores sobrevivem ao processo (Redis ou Supabase)."""
    return _get_redis_client() is not None or db.is_configured()


def daily_report(days: int = 14) -> list[dict]:
    """Linhas por dia (mais recente primeiro) com análises, custo e consenso rápido por modo."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    rows = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        modes = {}
        for mode in MODES:
            analyses = int(get_spend_on(f"obs:analises:{mode}", day))
            fallbacks = int(get_spend_on(f"obs:consenso_rapido:{mode}", day))
            modes[mode] = {
                "analises": analyses,
                "custo_usd": round(get_spend_on(f"obs:custo:{mode}", day), 4),
                "consenso_rapido": fallbacks,
            }
        total_analyses = sum(m["analises"] for m in modes.values())
        total_fallbacks = sum(m["consenso_rapido"] for m in modes.values())
        rows.append({
            "dia": day,
            "modos": modes,
            "analises": total_analyses,
            "custo_usd": round(sum(m["custo_usd"] for m in modes.values()), 4),
            "taxa_consenso_rapido": round(total_fallbacks / total_analyses, 3) if total_analyses else 0.0,
        })
    return rows

