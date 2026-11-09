"""
Marketing com consentimento: leads do teste grátis, descadastro e lembrete de
checkout abandonado.

Regras (LGPD):
- Lead só existe com opt-in explícito no /trial (checkbox desmarcado por
  padrão); a frase aceita fica gravada com o e-mail.
- Todo e-mail daqui leva link de descadastro e cabeçalho List-Unsubscribe.
  Quem se descadastra vai para a lista de supressão (email_descadastros) e não
  recebe mais lembrete nem marketing.
- O lembrete de checkout é sobre uma compra que a própria pessoa começou, um
  único envio por cobrança, e só depois de confirmar no Asaas que ela segue em
  aberto. Na dúvida (banco ou Asaas fora do ar), não envia.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature

from core import asaas, db, mailer
from web import security

logger = logging.getLogger(__name__)

DESCADASTRO_SALT = "email-descadastro-v1"

# Frase exibida ao lado do checkbox no /trial e gravada com o lead (prova do opt-in).
# Mudou a frase? Mude também no template: o servidor só aceita o texto vigente.
CONSENTIMENTO_MARKETING = (
    "Aceito receber por e-mail artigos, novidades e ofertas da Three Health. "
    "Posso cancelar quando quiser pelo link no rodapé dos e-mails."
)


# ── Descadastro ───────────────────────────────────────────────────────────────

def gerar_token_descadastro(email: str) -> str:
    """Token assinado e sem validade: link de descadastro precisa funcionar para sempre."""
    return security.serializer().dumps(email, salt=DESCADASTRO_SALT)


def email_do_token_descadastro(token: str) -> str | None:
    if not token:
        return None
    try:
        email = security.serializer().loads(token, salt=DESCADASTRO_SALT)
    except BadSignature:
        return None
    return email if isinstance(email, str) and "@" in email else None


def link_descadastro(email: str, base_url: str) -> str:
    return f"{base_url}/descadastrar?t={gerar_token_descadastro(email)}"


def cabecalhos_descadastro(link: str) -> dict[str, str]:
    """List-Unsubscribe com um clique (RFC 8058): o provedor faz POST no próprio link."""
    return {"List-Unsubscribe": f"<{link}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def mascarar_email(email: str) -> str:
    local, _, dominio = email.partition("@")
    visivel = local[:2] if len(local) > 2 else local[:1]
    return f"{visivel}{'•' * 3}@{dominio}"


# ── Lembrete de checkout abandonado ───────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _primeiro_nome(nome: str) -> str:
    partes = (nome or "").split()
    return partes[0].capitalize() if partes else ""


def montar_lembrete(nome: str, plano: str, invoice_url: str, base_url: str, link_sair: str) -> tuple[str, str]:
    """(assunto, corpo) do lembrete. Texto puro, sem dado de saúde nem de pagamento além do link."""
    saudacao = f"Olá, {_primeiro_nome(nome)}!" if _primeiro_nome(nome) else "Olá!"
    nome_plano = f"plano {plano.capitalize()}" if plano else "assinatura"
    nova_cobranca = f"{base_url}/checkout?plan={plano}&src=email_recuperacao" if plano else f"{base_url}/planos?src=email_recuperacao"

    linhas = [
        saudacao,
        "",
        f"Você começou a contratar o {nome_plano} do Three Health, mas o pagamento ainda não foi confirmado.",
        "",
    ]
    if invoice_url:
        linhas += ["Para concluir, é só pagar pelo link da cobrança:", invoice_url, ""]
    linhas += [
        "Se o link expirou ou você prefere outra forma de pagamento (PIX, boleto ou cartão), gere uma nova cobrança aqui:",
        nova_cobranca,
        "",
        "Assim que o pagamento for confirmado, o acesso é liberado na hora, com o e-mail e a senha que você criou.",
        "",
        "Equipe Three Health",
        "",
        "—",
        "Você recebeu este lembrete porque iniciou uma compra no Three Health.",
        f"Para não receber mais e-mails como este: {link_sair}",
    ]
    return "Falta pouco para liberar seu acesso ao Three Health", "\n".join(linhas)


def recuperar_checkouts_abandonados(agora: datetime | None = None) -> dict:
    """Envia um lembrete por cobrança em aberto na janela configurada. Retorna o resumo da execução."""
    resumo = {"encontrados": 0, "enviados": 0, "pulados": 0, "falhas": 0}
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if not mailer.is_configured() or not base_url:
        resumo["desligado"] = "Configure SMTP_HOST, SMTP_FROM e APP_BASE_URL."
        return resumo

    agora = agora or datetime.now(timezone.utc)
    min_horas = _env_int("CHECKOUT_RECOVERY_MIN_HOURS", 3)
    max_horas = max(_env_int("CHECKOUT_RECOVERY_MAX_HOURS", 72), min_horas)
    limite = _env_int("CHECKOUT_RECOVERY_BATCH", 25)

    linhas = db.listar_checkouts_abandonados(
        criado_depois_de=(agora - timedelta(hours=max_horas)).isoformat(),
        criado_antes_de=(agora - timedelta(hours=min_horas)).isoformat(),
        limite=limite,
    )
    resumo["encontrados"] = len(linhas)
    if not linhas:
        return resumo

    def _email(linha: dict) -> str:
        return ((linha.get("clientes") or {}).get("email") or "").strip().lower()

    descadastrados = db.emails_descadastrados(sorted({_email(l) for l in linhas if _email(l)}))
    if descadastrados is None:
        logger.error("Lista de descadastro indisponível — nenhum lembrete enviado.")
        resumo["falhas"] = len(linhas)
        return resumo

    ja_avisados: set[str] = set()
    for linha in linhas:
        payment_id = linha.get("asaas_payment_id") or ""
        email = _email(linha)

        # Sem cliente (conta excluída), descadastrado, já avisado nesta rodada ou
        # com outro plano já pago: marca para não voltar à fila e segue.
        if (
            not email
            or email in descadastrados
            or email in ja_avisados
            or db.buscar_pagamento_confirmado_por_email(email) is not None
        ):
            db.marcar_lembrete_enviado(payment_id)
            resumo["pulados"] += 1
            continue

        # O webhook pode ter atrasado: confirma no Asaas antes de cobrar alguém que já pagou.
        try:
            status = (asaas.get_payment(payment_id) or {}).get("status", "")
        except Exception:
            logger.warning("Não foi possível consultar a cobrança %s no Asaas; tenta amanhã.", payment_id, exc_info=True)
            resumo["falhas"] += 1
            continue
        if status not in db.STATUS_COBRANCA_EM_ABERTO:
            if status:
                db.atualizar_status_pagamento(payment_id, status)
            resumo["pulados"] += 1
            continue

        link_sair = link_descadastro(email, base_url)
        assunto, corpo = montar_lembrete(
            nome=(linha.get("clientes") or {}).get("nome", ""),
            plano=linha.get("plano") or "",
            invoice_url=linha.get("invoice_url") or "",
            base_url=base_url,
            link_sair=link_sair,
        )
        if mailer.send_email(email, assunto, corpo, headers=cabecalhos_descadastro(link_sair)):
            db.marcar_lembrete_enviado(payment_id)
            ja_avisados.add(email)
            resumo["enviados"] += 1
        else:
            resumo["falhas"] += 1

    logger.info("Recuperação de checkout: %s", resumo)
    return resumo

