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
