"""Plano ativo do cliente e emissão da sessão premium."""

import logging
import os
import time
from datetime import datetime, timezone
from typing import NamedTuple

from flask import g, request

from core import db
from web import config, security

logger = logging.getLogger(__name__)


class ActivePlan(NamedTuple):
    payment_id: str
    plan_key: str
    valid_until: float | None  # epoch; None = data de início desconhecida


def plan_key_from_reference(ext_ref: str | None) -> str:
    """external_reference é 'email|plano' (atual) ou só 'email' (legado)."""
    plan_key = ext_ref.split("|", 1)[1] if ext_ref and "|" in ext_ref else config.DEFAULT_PLAN
    return plan_key if plan_key in config.PLANS else config.DEFAULT_PLAN


def email_from_reference(ext_ref: str | None) -> str:
    return str(ext_ref or "").split("|", 1)[0].strip().lower()


def plan_valid_until(plan_key: str, inicio_iso: str | None) -> float | None:
    if not inicio_iso:
        return None
    try:
        inicio = datetime.fromisoformat(str(inicio_iso).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Data de início de plano ilegível: %r", inicio_iso)
        return None
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=config.LOCAL_TZ or timezone.utc)
    return inicio.timestamp() + config.PLANS[plan_key]["cookie_max_age"]


def find_active_plan(email: str) -> ActivePlan | None:
    """Plano ativo do e-mail: pagamento confirmado E dentro da validade do plano.

    Antes, qualquer pagamento confirmado — de qualquer data — reemitia o cookie
    no login, então o plano nunca expirava de fato.
    """
    result = db.buscar_pagamento_confirmado_por_email(email, planos=tuple(config.PLANS))
    if not result:
        return None
    payment_id, ext_ref, *rest = result
    plan_key = plan_key_from_reference(ext_ref)
    valid_until = plan_valid_until(plan_key, rest[0] if rest else None)
    if valid_until is not None and valid_until <= time.time():
        logger.info("Plano expirado (payment_id=%s, plano=%s)", payment_id, plan_key)
        return None
    return ActivePlan(payment_id, plan_key, valid_until)


def start_premium_session(resp, *, email: str, payment_id: str, plan_key: str, valid_until: float | None = None):
    """Emite o cookie premium (até o fim do plano) e registra a sessão no banco."""
    now = time.time()
    expires_at = now + config.PLANS[plan_key]["cookie_max_age"]
    if valid_until is not None:
        expires_at = min(expires_at, valid_until)
    max_age = max(int(expires_at - now), 0)

    sessao = db.buscar_sessao_cliente(email)
    token = security.generate_premium_token(
        payment_id, email, max_age, session_version=sessao["sessao_versao"] if sessao else 0
    )
    security.set_signed_cookie(resp, security.PREMIUM_COOKIE, token, max_age)

    cliente_id = sessao["id"] if sessao else None
    db.salvar_sessao(
        cliente_id=cliente_id,
        pagamento_id=db.buscar_pagamento_id(payment_id),
        ip_address=security.get_ip(),
        user_agent=request.headers.get("User-Agent"),
    )
    g.cliente_id = cliente_id

    # A requisição começou sem cookie premium: associa o UUID do cliente assim
    # que o login dá certo. Só o UUID vai ao PostHog — nunca e-mail ou nome.
    if cliente_id:
        from web.posthog import posthog_client

        if posthog_client is not None:
            posthog_client.identify_context(str(cliente_id))
            posthog_client.capture("premium_session_started", properties={"plan_key": plan_key})
    return resp


def app_base_url() -> str:
    """URL pública do app para links enviados por e-mail.

    Nunca derivada do cabeçalho Host em produção: um Host forjado faria o link
    (com token de acesso) apontar para o domínio do atacante.
    """
    configured = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    if config.is_local_runtime():
        return request.host_url.rstrip("/")
    return ""
