"""
Envio de e-mail transacional via SMTP (link de acesso / redefinição de senha).

Usa apenas a stdlib (smtplib). Configuração por env:
    SMTP_HOST, SMTP_PORT (padrão 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
Porta 465 usa SSL implícito; qualquer outra usa STARTTLS.

Sem SMTP_HOST/SMTP_FROM, `is_configured()` retorna False e o app deve
oferecer outro caminho (login com senha / suporte) em vez de liberar acesso
sem comprovar a posse do e-mail.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip() and os.environ.get("SMTP_FROM", "").strip())


def send_email(to: str, subject: str, body: str) -> bool:
    """Envia um e-mail de texto simples. Retorna True se o servidor SMTP aceitou."""
    if not is_configured():
        logger.warning("SMTP não configurado — e-mail não enviado.")
        return False

    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")

    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"].strip()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                smtp.starttls(context=context)
                if user:
                    smtp.login(user, password)
                smtp.send_message(message)
        return True
    except Exception:
        logger.exception("Falha ao enviar e-mail via SMTP")
        return False
