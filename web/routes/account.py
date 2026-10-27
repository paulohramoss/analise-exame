"""Conta do cliente: acesso, portabilidade e eliminação dos dados (LGPD art. 18)."""

import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, Response, flash, g, make_response, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash

from core import db, patient_report
from web import posthog, presentation, security
from web.extensions import rate_limit

logger = logging.getLogger(__name__)

bp = Blueprint("account", __name__)

DELETE_CONFIRMATION = "EXCLUIR"


def _own_account() -> tuple[str, str] | None:
    """(cliente_id, e-mail) de quem está logado como cliente — admin e acesso local não têm conta."""
    if g.get("is_admin") or not g.get("cliente_id") or not g.get("user_email"):
        return None
    return g.cliente_id, g.user_email


@bp.route("/conta")
@security.premium_required
def conta():
    """Página "Meus dados": exportação e exclusão da conta."""
    account = _own_account()
    return render_template(
        "conta.html",
        email=g.get("user_email"),
        has_account=account is not None,
        confirmation_word=DELETE_CONFIRMATION,
        backup=_prontuario_backup(account[0]) if account else None,
        # A marca vale para quem tem cliente_id — inclusive o admin, na conta própria.
        pode_personalizar=bool(g.get("cliente_id")),
        marca=db.buscar_marca_clinica(g.get("cliente_id")) if g.get("cliente_id") else {},
    )


@bp.route("/conta/clinica", methods=["POST"])
@security.premium_required
@rate_limit("ACCOUNT_BRANDING_RATE_LIMIT", "30 per hour")
def salvar_clinica():
    """Nome, contato e logo da clínica, impressos no relatório para o paciente."""
    cliente_id = g.get("cliente_id")
    if not cliente_id:
        flash("Disponível para contas de profissional.", "error")
        return redirect(url_for("account.conta"))

    campos = {
        "clinica_nome": presentation.form_text("clinica_nome", 120) or None,
        "clinica_contato": presentation.form_text("clinica_contato", 160) or None,
    }
    logo = request.files.get("clinica_logo")
    if request.form.get("remover_logo") == "1":
        campos["clinica_logo"] = None
    elif logo and logo.filename:
        data_url = patient_report.processar_logo(logo.read(patient_report.LOGO_MAX_BYTES + 1))
        if data_url is None:
            flash("Não foi possível usar o logo. Envie uma imagem PNG ou JPG de até 2 MB.", "error")
            return redirect(url_for("account.conta") + "#clinica")
        campos["clinica_logo"] = data_url

    if db.salvar_marca_clinica(cliente_id, campos):
        flash("Dados da clínica salvos. Eles aparecem no relatório para o paciente.", "success")
    else:
        flash("Não foi possível salvar os dados da clínica agora.", "error")
    return redirect(url_for("account.conta") + "#clinica")


def _prontuario_backup(cliente_id: str) -> dict:
    """Situação da cópia dos prontuários. Sem resposta do banco, assume que não há cópia em dia."""
    return db.resumo_backup_prontuarios(cliente_id) or {"pacientes": None, "exportado_em": None, "copia_em_dia": False}


@bp.route("/conta/dados.json")
@security.premium_required
@rate_limit("ACCOUNT_EXPORT_RATE_LIMIT", "10 per hour")
def exportar_dados():
    """Entrega ao titular uma cópia dos seus dados em JSON (LGPD art. 18, II e V)."""
    account = _own_account()
    if account is None:
        flash("Exportação disponível apenas para contas de cliente.", "error")
        return redirect(url_for("account.conta"))

    dados = db.exportar_dados_cliente(account[0])
    if dados is None:
        flash("Não foi possível gerar a exportação agora. Tente novamente em instantes.", "error")
        return redirect(url_for("account.conta"))

    dados = {"gerado_em": datetime.now(timezone.utc).isoformat(), **dados}
    db.marcar_exportacao_dados(account[0])
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture("account_data_exported")
    filename = f"three-health-meus-dados-{datetime.now(timezone.utc):%Y%m%d}.json"
    return Response(
        json.dumps(dados, ensure_ascii=False, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@bp.route("/conta/excluir", methods=["POST"])
@security.premium_required
@rate_limit("ACCOUNT_DELETE_RATE_LIMIT", "5 per hour")
def excluir_conta():
    """Elimina a conta e os dados pessoais, depois de confirmar senha e intenção."""
    account = _own_account()
    if account is None:
        flash("Exclusão disponível apenas para contas de cliente.", "error")
        return redirect(url_for("account.conta"))
    cliente_id, email = account

    if request.form.get("confirmacao", "").strip().upper() != DELETE_CONFIRMATION:
        flash(f"Para confirmar, digite {DELETE_CONFIRMATION} no campo indicado.", "error")
        return redirect(url_for("account.conta"))

    # Os prontuários dos pacientes são apagados junto com a conta. Sem uma cópia
    # baixada que cubra a última alteração, a exclusão exige ciência explícita.
    if not _prontuario_backup(cliente_id)["copia_em_dia"] and request.form.get("ciente_prontuarios") != "accepted":
        flash(
            "Você não baixou a cópia mais recente dos prontuários dos seus pacientes. "
            "Baixe seus dados ou confirme que entende que os prontuários serão apagados sem cópia.",
            "error",
        )
        return redirect(url_for("account.conta"))

    senha_hash = db.buscar_senha_hash_cliente(email)
    if not senha_hash or not check_password_hash(senha_hash, request.form.get("senha", "")):
        flash("Senha incorreta.", "error")
        return redirect(url_for("account.conta"))

    if not db.excluir_dados_cliente(cliente_id):
        flash("Não foi possível excluir os dados agora. Tente novamente ou fale com o suporte.", "error")
        return redirect(url_for("account.conta"))

    logger.info("Conta excluída a pedido do titular (cliente_id=%s)", cliente_id)
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture("account_deleted")
    security.forget_session(email)
    flash("Sua conta e seus dados foram excluídos.", "success")
    resp = make_response(redirect(url_for("analysis.index")))
    resp.delete_cookie(security.PREMIUM_COOKIE)
    return resp
