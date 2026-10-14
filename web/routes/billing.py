"""Planos, checkout (Asaas), confirmação de pagamento e webhook."""

import hmac
import logging
import os

from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from core import asaas, db
from core.email_validation import validar_email_cadastro
from web import accounts, config, security

logger = logging.getLogger(__name__)

bp = Blueprint("billing", __name__)

# Estorno/chargeback/exclusão: derrubam na hora os cookies premium já emitidos.
REVOKING_PAYMENT_EVENTS = {
    "PAYMENT_REFUNDED",
    "PAYMENT_PARTIALLY_REFUNDED",
    "PAYMENT_REFUND_IN_PROGRESS",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_CHARGEBACK_DISPUTE",
    "PAYMENT_DELETED",
}


@bp.route("/planos")
def planos():
    """Página pública de comparação de planos — usada na LP e no site."""
    return render_template("planos.html", plans=config.PLANS)


@bp.route("/checkout")
def checkout():
    """Página de contratação com formulário de nome + e-mail."""
    if security.is_premium(request):
        return redirect(url_for("analysis.index"))
    plan = request.args.get("plan", config.DEFAULT_PLAN)
    if plan not in config.PLANS:
        plan = config.DEFAULT_PLAN
    return render_template("checkout.html", plans=config.PLANS, default_plan=plan)


@bp.route("/checkout/pay", methods=["POST"])
def checkout_pay():
    """Cria cliente + cobrança no Asaas. Retorna JSON com dados de pagamento inline.

    Cartão: a cobrança é criada sem dados do cartão e o cliente paga na página
    segura do Asaas (invoiceUrl). Número, validade e CVV nunca passam por este
    servidor — antes passavam, o que colocava o app inteiro no escopo do PCI-DSS.
    """
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    cpf_cnpj = request.form.get("cpf_cnpj", "").strip()
    plan_key = request.form.get("plan", config.DEFAULT_PLAN)
    billing_type = request.form.get("billing_type", "PIX").upper()
    senha_acesso = request.form.get("senha_acesso", "").strip()
    senha_confirmar = request.form.get("senha_confirmar", "").strip()

    if plan_key not in config.PLANS:
        plan_key = config.DEFAULT_PLAN
    plan = config.PLANS[plan_key]
    if billing_type not in {"PIX", "BOLETO", "CREDIT_CARD"}:
        billing_type = "PIX"

    def _error(message: str, status: int):
        return jsonify({"success": False, "error": message}), status

    # ── Validações básicas ────────────────────────────────────────────────────
    if not name:
        return _error("Preencha o nome.", 400)

    # Sintaxe + blocklist de descartáveis + DNS do domínio. Também normaliza
    # (minúsculo) — o login e o /acesso já buscam pelo e-mail em minúsculo.
    email, erro_email = validar_email_cadastro(email)
    if erro_email:
        return _error(erro_email, 400)
    if not cpf_cnpj:
        return _error("Preencha o CPF ou CNPJ.", 400)
    if not senha_acesso or len(senha_acesso) < 8:
        return _error("A senha deve ter pelo menos 8 caracteres.", 400)
    if senha_acesso != senha_confirmar:
        return _error("As senhas não coincidem.", 400)
    if not os.environ.get("ASAAS_API_KEY", ""):
        return _error("Serviço de pagamento indisponível. Tente mais tarde.", 503)

    external_reference = f"{email}|{plan_key}"

    # ── Criação no Asaas ──────────────────────────────────────────────────────
    try:
        customer = asaas.create_customer(name=name, email=email, cpf_cnpj=cpf_cnpj)
        payment = asaas.create_payment(
            customer_id=customer["id"],
            value=plan["price"],
            description=plan["name"],
            external_reference=external_reference,
            billing_type=billing_type,
        )
    except Exception as e:
        # Detalhes (status, corpo da resposta do Asaas) só no log — nunca ao usuário.
        logger.exception("Erro ao criar cobrança Asaas (%s)", type(e).__name__)
        err_lower = str(e).lower()
        if "timeout" in err_lower or "connection" in err_lower:
            return _error("Não foi possível conectar ao serviço de pagamento. Tente novamente.", 502)
        if "cpf" in err_lower or "cnpj" in err_lower:
            return _error("CPF ou CNPJ inválido. Confira o número informado.", 502)
        return _error("Não foi possível gerar a cobrança agora. Tente novamente em instantes.", 502)

    payment_id = payment.get("id", "")
    invoice_url = payment.get("invoiceUrl") or payment.get("bankSlipUrl") or ""
    if not payment_id:
        return _error("Erro ao criar cobrança. Tente novamente.", 502)

    # ── Persistência ──────────────────────────────────────────────────────────
    cliente_id = db.upsert_cliente(name, email, cpf_cnpj, customer.get("id", ""))
    g.cliente_id = cliente_id
    db.salvar_senha_cliente(email, generate_password_hash(senha_acesso))
    db.salvar_pagamento(
        asaas_payment_id=payment_id,
        valor=plan["price"],
        descricao=plan["name"],
        invoice_url=invoice_url,
        external_reference=external_reference,
        cliente_id=cliente_id,
        payload=payment,
        plano=plan_key,
        forma_pagamento=billing_type,
    )

    # ── Resposta por tipo de pagamento ────────────────────────────────────────
    body = {"success": True, "billing_type": billing_type, "payment_id": payment_id}
    if billing_type == "PIX":
        try:
            pix = asaas.get_pix_qr_code(payment_id)
        except Exception:
            logger.exception("Erro ao obter QR PIX (payment_id=%s)", payment_id)
            pix = {}
        body.update(pix_qr_image=pix.get("encodedImage", ""), pix_copy_paste=pix.get("payload", ""))
    elif billing_type == "BOLETO":
        try:
            boleto = asaas.get_boleto_identification(payment_id)
        except Exception:
            logger.exception("Erro ao obter identificação de boleto (payment_id=%s)", payment_id)
            boleto = {}
        body.update(boleto_barcode=boleto.get("identificationField", ""), boleto_url=invoice_url)
    else:
        # CREDIT_CARD: pagamento na página do Asaas; a confirmação chega pelo
        # polling de /api/payment/status, como no PIX e no boleto.
        body["invoice_url"] = invoice_url
    return security.with_checkout_owner(jsonify(body), payment_id)


@bp.route("/checkout/pending")
def checkout_pending():
    """Aguarda confirmação do pagamento com polling automático."""
    payment_id = request.args.get("payment_id", "")
    invoice_url = request.args.get("invoice_url", "")
    if not payment_id:
        return redirect(url_for("billing.checkout"))
    return render_template("checkout_pending.html", payment_id=payment_id, invoice_url=invoice_url)


@bp.route("/api/payment/status")
def payment_status():
    """Verifica o status de uma cobrança no Asaas (usado pelo polling do frontend)."""
    payment_id = request.args.get("id", "").strip()
    if not payment_id:
        return jsonify({"confirmed": False, "error": "ID não informado"}), 400

    try:
        confirmed = asaas.is_payment_confirmed(payment_id)
    except Exception:
        logger.exception("Erro ao verificar pagamento Asaas (payment_id=%s)", payment_id)
        return jsonify({"confirmed": False, "error": "Erro ao consultar pagamento"}), 502
    if not confirmed:
        return jsonify({"confirmed": False})

    db.atualizar_status_pagamento(payment_id, "CONFIRMED")
    login_required = jsonify({"confirmed": True, "login_required": True})

    # Quem só conhece o payment_id (link compartilhado, outro aparelho)
    # vê a confirmação, mas entra pelo login com a senha criada no checkout.
    if not security.owns_checkout(request, payment_id):
        return login_required

    try:
        ext_ref = asaas.get_payment(payment_id).get("externalReference", "")
    except Exception:
        logger.exception("Erro ao obter cobrança Asaas (payment_id=%s)", payment_id)
        return login_required
    email = accounts.email_from_reference(ext_ref)
    if not email:
        return login_required

    # setup_token só é necessário se o cliente ainda não tem senha (checkout sempre define)
    setup_token = "" if db.buscar_senha_hash_cliente(email) else security.generate_setup_token(email)
    return accounts.start_premium_session(
        jsonify({"confirmed": True, "setup_token": setup_token}),
        email=email,
        payment_id=payment_id,
        plan_key=accounts.plan_key_from_reference(ext_ref),
    )


@bp.route("/webhook/asaas", methods=["POST"])
def webhook_asaas():
    """
    Recebe notificações de pagamento do Asaas.
    Persiste o evento e atualiza o status do pagamento no banco.
    """
    # Fora do ambiente local o token é obrigatório: sem ele, qualquer um poderia
    # marcar cobranças como pagas/estornadas forjando o corpo do evento.
    webhook_token = os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()
    if not webhook_token:
        if not config.is_local_runtime():
            logger.error("ASAAS_WEBHOOK_TOKEN não configurado — webhook do Asaas recusado.")
            return jsonify({"error": "Webhook não configurado"}), 503
    else:
        received_token = request.headers.get("asaas-access-token", "")
        if not hmac.compare_digest(received_token.encode("utf-8"), webhook_token.encode("utf-8")):
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    event = data.get("event", "")
    payment = data.get("payment", {})
    payment_id = payment.get("id", "")
    status = payment.get("status", "")

    logger.info("Webhook Asaas recebido", extra={"event": event, "payment_id": payment_id, "status": status})

    db.salvar_webhook(evento=event, asaas_payment_id=payment_id, status_pagamento=status, payload=data)
    if payment_id and status:
        db.atualizar_status_pagamento(payment_id, status, payload=data)

    # O status novo também tira o pagamento da busca de plano ativo (login).
    if event in REVOKING_PAYMENT_EVENTS:
        email = accounts.email_from_reference(payment.get("externalReference"))
        if email:
            security.revoke_sessions(email)
            logger.info("Sessões premium revogadas por %s (payment_id=%s)", event, payment_id)

    return jsonify({"received": True}), 200
