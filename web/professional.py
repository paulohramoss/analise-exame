"""Perfil profissional da conta e termo de responsabilidade.

O termo é aceito uma vez por versão e por conta (clientes.termo_versao). Mudou
o texto de forma relevante? Suba TERMO_VERSAO: todo mundo aceita de novo na
próxima análise. Admin e acesso local não têm conta e não passam pelo termo.
"""

import logging

from flask import g, request

from core import db
from core import professional_register as registro

logger = logging.getLogger(__name__)

TERMO_VERSAO = "2026-11"


def _tem_conta() -> bool:
    return bool(g.get("cliente_id")) and not g.get("is_admin")


def perfil_da_conta() -> dict:
    """Perfil salvo na conta ({} sem conta ou se o banco não responder)."""
    if not g.get("cliente_id"):
        return {}
    if "perfil_profissional" not in g:
        g.perfil_profissional = db.buscar_perfil_profissional(g.cliente_id)
    return g.perfil_profissional or {}


def responsavel_da_conta() -> dict[str, str]:
    """Perfil da conta no formato do responsável do laudo (preenche o formulário)."""
    perfil = perfil_da_conta()
    return {
        "name": perfil.get("profissional_nome") or "",
        "role": perfil.get("profissional_perfil") or "",
        "register": perfil.get("profissional_registro") or "",
        "organization": perfil.get("profissional_instituicao") or "",
    }


def termo_pendente() -> bool:
    """A conta ainda não aceitou a versão atual do termo."""
    if not _tem_conta():
        return False
    return perfil_da_conta().get("termo_versao") != TERMO_VERSAO


def aceite_no_formulario() -> bool:
    value = (request.form.get("termo_responsabilidade") or "").strip().lower()
    return value in {"accepted", "on", "true", "1", "yes"}


def exigir_termo() -> str | None:
    """Mensagem de erro se o termo for exigido e não aceito; grava o aceite marcado no formulário."""
    if not termo_pendente():
        return None
    if not aceite_no_formulario():
        return "Leia e aceite o termo de responsabilidade profissional antes de enviar o exame."
    if not db.registrar_aceite_termo(g.cliente_id, TERMO_VERSAO):
        # Sem a migration ou com o banco fora: a análise segue (o aceite veio no
        # formulário) e o termo volta a ser pedido na próxima.
        logger.warning("Aceite do termo não gravado; será pedido de novo.")
    else:
        g.perfil_profissional = {**perfil_da_conta(), "termo_versao": TERMO_VERSAO}
    return None


def validar_responsavel(info: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """Confere o registro do responsável pelo perfil escolhido e devolve-o normalizado."""
    normalizado, erro = registro.validar_registro(info.get("role", ""), info.get("register", ""))
    return {**info, "register": normalizado}, erro
