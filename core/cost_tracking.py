"""
Custo estimado por análise (tokens × preço) e orçamentos de gasto diário.

Não faz nenhuma chamada de rede: apenas calcula custo a partir de contagens de
tokens já retornadas pelas APIs do Gemini/Claude, loga o resultado de forma
estruturada, e mantém contadores de gasto diário por "escopo" (ex.: trial
global, um usuário específico) para permitir bloquear análises quando um
limite configurado é excedido.

Armazenamento dos contadores: Redis quando RATELIMIT_STORAGE_URI aponta para
um Redis (mesma infra já usada pelo flask-limiter), com fallback em memória do
processo. Em memória, o limite só é efetivo dentro de uma única instância —
suficiente para desenvolvimento local, mas em produção com múltiplas instâncias
(Vercel) configure Redis para um teto realmente global.
"""

import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Preços aproximados em USD por 1 milhão de tokens. Mudam com frequência —
# configuráveis via env para não exigir deploy a cada ajuste de tabela dos
# provedores. Os defaults abaixo são apenas uma referência aproximada.
_PRICING_PER_1M_TOKENS = {
    "gemini-2.5-flash": (
        float(os.environ.get("GEMINI_2_5_FLASH_INPUT_PRICE_PER_1M", "0.30")),
        float(os.environ.get("GEMINI_2_5_FLASH_OUTPUT_PRICE_PER_1M", "2.50")),
    ),
    "gemini-2.0-flash": (
        float(os.environ.get("GEMINI_2_0_FLASH_INPUT_PRICE_PER_1M", "0.10")),
        float(os.environ.get("GEMINI_2_0_FLASH_OUTPUT_PRICE_PER_1M", "0.40")),
    ),
    "gemini-2.0-flash-lite": (
        float(os.environ.get("GEMINI_2_0_FLASH_LITE_INPUT_PRICE_PER_1M", "0.075")),
        float(os.environ.get("GEMINI_2_0_FLASH_LITE_OUTPUT_PRICE_PER_1M", "0.30")),
    ),
    "claude-sonnet-4-6": (
        float(os.environ.get("CLAUDE_SONNET_INPUT_PRICE_PER_1M", "3.00")),
        float(os.environ.get("CLAUDE_SONNET_OUTPUT_PRICE_PER_1M", "15.00")),
    ),
}
_ZERO_PRICING = (0.0, 0.0)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Custo estimado em USD para uma chamada com as contagens de tokens dadas."""
    input_price, output_price = _PRICING_PER_1M_TOKENS.get(model, _ZERO_PRICING)
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


def _get_redis_client():
    """Reaproveita RATELIMIT_STORAGE_URI (já usado pelo flask-limiter) se for Redis."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    uri = os.environ.get("RATELIMIT_STORAGE_URI", "").strip()
    if not uri.startswith("redis://") and not uri.startswith("rediss://"):
        return None
    try:
        import redis
        _redis_client = redis.from_url(uri)
        _redis_client.ping()
    except Exception:
        logger.warning("Não foi possível conectar ao Redis para orçamento de custo; usando memória local.", exc_info=True)
        _redis_client = None
    return _redis_client


def add_spend(scope: str, amount_usd: float) -> float:
    """Soma `amount_usd` ao gasto acumulado do dia (UTC) para `scope`. Retorna o total atualizado."""
    if amount_usd <= 0:
        return get_spend(scope)

    redis_client = _get_redis_client()
    if redis_client is not None:
        key = f"cost_budget:{scope}:{_today()}"
        try:
            total = redis_client.incrbyfloat(key, amount_usd)
            redis_client.expire(key, 2 * 24 * 3600)
            return float(total)
        except Exception:
            logger.warning("Falha ao registrar gasto no Redis; usando memória local para este processo.", exc_info=True)

    with _memory_lock:
        day, total = _memory_spend.get(scope, (_today(), 0.0))
        if day != _today():
            total = 0.0
        total += amount_usd
        _memory_spend[scope] = (_today(), total)
        return total


def get_spend(scope: str) -> float:
    """Gasto acumulado hoje (UTC) para `scope`."""
    redis_client = _get_redis_client()
    if redis_client is not None:
        key = f"cost_budget:{scope}:{_today()}"
        try:
            value = redis_client.get(key)
            return float(value) if value is not None else 0.0
        except Exception:
            logger.warning("Falha ao consultar gasto no Redis; usando memória local.", exc_info=True)

    with _memory_lock:
        day, total = _memory_spend.get(scope, (_today(), 0.0))
        return total if day == _today() else 0.0


def is_budget_exceeded(scope: str, cap_usd: float) -> bool:
    """cap_usd <= 0 desativa o limite (sempre retorna False)."""
    if cap_usd <= 0:
        return False
    return get_spend(scope) >= cap_usd
