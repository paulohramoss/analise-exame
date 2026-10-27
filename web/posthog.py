"""
Cliente PostHog do processo (web e worker): eventos de produto, exceções e
métricas das chamadas de IA.

Privacidade (dado de saúde é dado sensível — LGPD art. 11):
- privacy_mode=True: os wrappers de IA (Gemini/Claude) registram modelo,
  tokens, latência e custo, mas NUNCA o prompt, as imagens do exame ou o laudo
  ($ai_input / $ai_output_choices / $ai_instructions saem vazios);
- capture_exception_code_variables=False: exceções vão sem variáveis locais
  (que poderiam conter a descrição clínica ou o laudo);
- a pessoa é identificada só pelo UUID do cliente — nunca e-mail, nome ou CPF;
- não há posthog-js no navegador (sem session replay das telas de exame).

Sem POSTHOG_PROJECT_TOKEN/POSTHOG_HOST a integração fica desligada (com um
aviso no log) — o app, os testes e o worker funcionam normalmente.
"""

import atexit
import logging
import os
import re
import uuid

from posthog import Posthog

logger = logging.getLogger(__name__)

posthog_client: Posthog | None = None
_warned_missing_config = False


def init_posthog() -> Posthog | None:
    """Inicializa (uma vez por processo) e devolve o cliente PostHog, ou None se não configurado."""
    global posthog_client, _warned_missing_config

    if posthog_client is not None:
        return posthog_client

    project_token = os.environ.get("POSTHOG_PROJECT_TOKEN", "").strip()
    host = os.environ.get("POSTHOG_HOST", "").strip()
    if not project_token or not host:
        if not _warned_missing_config:
            _warned_missing_config = True
            logger.warning("PostHog desativado: defina POSTHOG_PROJECT_TOKEN e POSTHOG_HOST para enviar eventos.")
        return None

    posthog_client = Posthog(
        project_api_key=project_token,
        host=host,
        enable_exception_autocapture=True,
        capture_exception_code_variables=False,
        privacy_mode=True,
    )
    atexit.register(posthog_client.shutdown)
    return posthog_client


def analysis_not_saved(analise_id: str | None, mode: str, cliente_id: str | None = None) -> bool:
    """True se havia banco e o laudo não foi gravado — e avisa no log e no PostHog.

    A falha de gravação não interrompe a análise (o profissional ainda vê o
    laudo), então sem este aviso ela passa despercebida: em 23/10/2026 uma
    coluna faltando no banco fez todos os laudos deixarem de ser salvos por dias.
    O evento `analysis_save_failed` serve para um alerta no PostHog.
    """
    from core import db

    if analise_id or not db.is_configured():
        return False
    logger.error("Laudo NÃO foi salvo no banco (mode=%s) — ver 'Erro em salvar_analise' acima.", mode)
    client = init_posthog()
    if client is not None:
        client.capture(
            "analysis_save_failed",
            distinct_id=str(cliente_id) if cliente_id else None,
            properties={"mode": mode},
        )
    return True


# ── Funil de vendas ───────────────────────────────────────────────────────────
# Sem posthog-js no navegador (ver docstring), o funil é medido no servidor.
# Quem ainda não tem conta é identificado por um UUID aleatório num cookie
# próprio (sem nome, e-mail ou IP); no checkout esse UUID é ligado ao UUID do
# cliente (alias), e o PostHog junta teste gratuito → checkout → pagamento.
VISITOR_COOKIE = "th_vid"
VISITOR_COOKIE_MAX_AGE = 365 * 24 * 3600
_BOT_MARKERS = ("bot", "crawler", "spider", "slurp", "preview", "monitor", "headless")
_SOURCE_RE = re.compile(r"[^a-z0-9_]")


def visitor_id_from_cookie(value: str | None) -> str | None:
    """O UUID do cookie, ou None se ausente/adulterado (só aceitamos UUID)."""
    try:
        return str(uuid.UUID(value or ""))
    except ValueError:
        return None


def new_visitor_id() -> str:
    return str(uuid.uuid4())


def source_param(value: str | None) -> str:
    """?src= dos links internos (de onde a pessoa veio), limpo e curto."""
    return _SOURCE_RE.sub("", (value or "").lower())[:40] or "direto"


def _is_bot(user_agent: str | None) -> bool:
    ua = (user_agent or "").lower()
    return not ua or any(marker in ua for marker in _BOT_MARKERS)


def track(event: str, **properties) -> None:
    """Evento do funil na identidade da requisição (cliente ou visitante). Ignora robôs."""
    from flask import request

    if posthog_client is None or _is_bot(request.headers.get("User-Agent")):
        return
    posthog_client.capture(event, properties=properties)


def link_visitor(visitor_id: str | None, cliente_id: str | None) -> None:
    """Liga o histórico anônimo do visitante ao cliente que acabou de se cadastrar."""
    if posthog_client is None or not visitor_id or not cliente_id or visitor_id == str(cliente_id):
        return
    posthog_client.alias(previous_id=visitor_id, distinct_id=str(cliente_id))
