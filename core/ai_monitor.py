"""
Monitoramento dos erros de IA, com alerta — não só log.

Conta, por dia, as falhas que deixam o laudo ruim ou ausente sem que ninguém
perceba: extração vazia (o modelo não achou valores), JSON inválido (marcações
ou extração), erro do provedor e análise lenta. Passou do limite, dispara um
alerta uma vez por dia por tipo: `logger.error` (vai para o Sentry) e, com
AI_ALERT_EMAIL + SMTP configurados, um e-mail para a equipe.

Os contadores usam o mesmo armazenamento do orçamento (Redis → Supabase →
memória), então o alerta vale para todas as instâncias e aparece em
/admin/custos. Nada de conteúdo clínico nem dado do paciente vai no alerta.
"""

import logging
import os

logger = logging.getLogger(__name__)

TIPOS = {
    "extracao_vazia": "extração sem valores (laboratorial/dinamometria)",
    "json_invalido": "resposta da IA sem JSON válido",
    "erro_provedor": "erro ou timeout do provedor de IA",
    "lenta": "análise acima do tempo limite",
}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def limite_alerta() -> int:
    """Falhas de um mesmo tipo no dia que disparam o alerta."""
    return max(1, _int_env("AI_ALERT_THRESHOLD", 5))


def limite_lenta_segundos() -> int:
    return max(1, _int_env("AI_SLOW_ANALYSIS_SECONDS", 90))


def registrar(tipo: str, detalhe: str = "") -> None:
    """Conta a falha e alerta se o tipo passou do limite hoje. Nunca levanta exceção."""
    if tipo not in TIPOS:
        return
    try:
        from core import cost_tracking

        total = cost_tracking.increment_daily_counter(f"obs:falha_ia:{tipo}")
        logger.warning("Falha de IA registrada: %s (%s) — %d hoje.", tipo, detalhe[:120], int(total))
        if total >= limite_alerta() and cost_tracking.increment_daily_counter(f"obs:alerta_falha_ia:{tipo}") == 1:
            _alertar(tipo, int(total), detalhe)
    except Exception:
        logger.exception("Falha ao registrar erro de IA no monitor.")


def registrar_duracao(segundos: float) -> None:
    if segundos >= limite_lenta_segundos():
        registrar("lenta", f"{segundos:.0f}s")


def _alertar(tipo: str, total: int, detalhe: str) -> None:
    mensagem = (
        f"Alerta de IA: {total} ocorrências hoje de '{TIPOS[tipo]}' (limite {limite_alerta()}). "
        f"Último detalhe: {detalhe[:200] or '—'}. Veja /admin/custos e os logs."
    )
    # logger.error chega ao Sentry quando SENTRY_DSN está configurado.
    logger.error(mensagem)
    destino = os.environ.get("AI_ALERT_EMAIL", "").strip()
    if not destino:
        return
    from core import mailer

    if mailer.is_configured():
        mailer.send_email(destino, f"[Three Health] Alerta de IA: {TIPOS[tipo]}", mensagem)


def resumo_do_dia() -> list[dict]:
    """[{tipo, descricao, total, alertado}] para o painel interno."""
    from core import cost_tracking

    return [
        {
            "tipo": tipo,
            "descricao": descricao,
            "total": int(cost_tracking.get_daily_counter(f"obs:falha_ia:{tipo}")),
            "alertado": cost_tracking.get_daily_counter(f"obs:alerta_falha_ia:{tipo}") >= 1,
        }
        for tipo, descricao in TIPOS.items()
    ]
