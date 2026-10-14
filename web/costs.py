"""
Orçamento de custo diário (Gemini + Claude) e cota do trial por IP.

Protege contra estouro de custo (ex.: abuso do trial gratuito) limitando o
gasto diário por escopo. Cada teto é opcional (0 = desativado); o do trial vem
habilitado por padrão por ser o vetor de abuso mais óbvio.
"""

import hashlib
import logging
import os

from flask import g, request

from core import cost_tracking
from web import security

logger = logging.getLogger(__name__)


def budget_user_key(mode: str) -> str | None:
    """Identificador do 'usuário' para o teto por usuário, por modo."""
    if mode == "premium":
        return g.get("user_email") or None
    if mode == "api":
        supplied_key = request.headers.get("X-API-Key", "").strip()
        if not supplied_key:
            return None
        return f"apikey:{hashlib.sha256(supplied_key.encode('utf-8')).hexdigest()[:16]}"
    return None


def cost_budget_scopes(mode: str) -> list[tuple[str, float]]:
    """Escopos [(scope, cap_usd), ...] a verificar/atualizar para esta análise."""
    scopes = []
    global_cap = float(os.environ.get("GLOBAL_DAILY_COST_BUDGET_USD", "0"))
    if global_cap > 0:
        scopes.append(("global", global_cap))

    if mode == "trial":
        trial_cap = float(os.environ.get("TRIAL_DAILY_COST_BUDGET_USD", "3.00"))
        if trial_cap > 0:
            scopes.append(("trial", trial_cap))

    user_cap = float(os.environ.get("USER_DAILY_COST_BUDGET_USD", "0"))
    user_key = budget_user_key(mode)
    if user_key and user_cap > 0:
        scopes.append((f"user:{user_key}", user_cap))

    return scopes


def blocked_by_cost_budget(mode: str) -> bool:
    for scope, cap in cost_budget_scopes(mode):
        if cost_tracking.is_budget_exceeded(scope, cap):
            logger.warning("Bloqueado por orçamento de custo diário excedido (scope=%s, cap_usd=%.2f)", scope, cap)
            return True
    return False


def record_analysis_cost(mode: str, result: dict) -> None:
    """Loga o custo total da análise e o credita nos orçamentos ativos."""
    usage = result.get("usage", [])
    cost_tracking.log_analysis_cost(result.get("exam_type", ""), result.get("model_used", ""), usage)

    cost_usd = sum(
        cost_tracking.estimate_cost_usd(item["model"], item["input_tokens"], item["output_tokens"])
        for item in usage
    )
    if cost_usd > 0:
        for scope, _ in cost_budget_scopes(mode):
            cost_tracking.add_spend(scope, cost_usd)
    cost_tracking.record_outcome(mode, result.get("model_used", ""), cost_usd)


def trial_limit_per_ip() -> int:
    """Análises gratuitas por IP por dia (0 desativa). O contador do navegador
    (localStorage) é só visual: limpar o navegador zerava o limite."""
    try:
        return max(0, int(os.environ.get("TRIAL_MAX_PER_IP_PER_DAY", "5")))
    except ValueError:
        return 5


def trial_quota_scope() -> str:
    # IP com hash (com o segredo do app): o contador não guarda o IP em claro.
    return f"trial_ip:{security.hash_sensitive_value(security.get_ip() or 'desconhecido')}"
