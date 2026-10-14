"""Login, definição de senha, recuperação de acesso e acesso admin."""

import hmac
import logging

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from core import cost_tracking, db, mailer
from core.email_validation import validar_email_cadastro
from web import accounts, config, security
from web.extensions import rate_limit

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

ACESSO_LINK_ENVIADO = (
    "Se houver um plano ativo para este e-mail, enviamos um link de acesso. "
    "Confira sua caixa de entrada (e o spam) — o link vale por 24 horas."
)
_CREDENCIAIS_INVALIDAS = "E-mail ou senha inválidos. Se ainda não criou sua senha, use “Recuperar acesso”."


# ── Acesso admin (testes internos) ────────────────────────────────────────────

@bp.route("/admin/entrar")
def admin_entrar():
    """
    Concede acesso de teste via cookie admin, protegido pela variável ADMIN_KEY.
    Em ambiente local, sem ADMIN_KEY, uma chave temporária é gerada por processo
    e registrada no log de inicialização (ver config.GENERATED_LOCAL_ADMIN_KEY).
    Uso: /admin/entrar?key=SUA_ADMIN_KEY — para revogar: /admin/sair
    """
    admin_key = config.get_admin_key()
    if not admin_key:
        return "ADMIN_KEY não configurada no servidor. Defina ADMIN_KEY nas variáveis de ambiente.", 403

    provided = request.args.get("key", "").strip()
    if not provided or not hmac.compare_digest(provided, admin_key):
        return "Chave inválida.", 403

    token = security.serializer().dumps({"admin": True}, salt=security.ADMIN_SALT)
    resp = make_response(redirect(url_for("analysis.index")))
    return security.set_signed_cookie(resp, security.ADMIN_COOKIE, token, security.ADMIN_COOKIE_MAX_AGE)


@bp.route("/admin/custos")
def admin_custos():
    """Painel interno: análises, custo estimado e taxa de "consenso rápido" por dia e modo."""
    if not security.is_admin(request):
        return "Acesso restrito.", 403
    try:
        days = max(1, min(int(request.args.get("dias", "14")), 90))
    except ValueError:
        days = 14
    rows = cost_tracking.daily_report(days)
    ratio_limit, minimum = cost_tracking.fallback_alert_settings()
    return render_template(
        "admin_custos.html",
        rows=rows,
        days=days,
        modes=cost_tracking.MODES,
        alert_ratio=ratio_limit,
        alert_minimum=minimum,
        persistent=cost_tracking.has_persistent_storage(),
    )


@bp.route("/admin/sair")
def admin_sair():
    """Remove o cookie de acesso admin."""
    resp = make_response(redirect(url_for("analysis.index")))
    resp.delete_cookie(security.ADMIN_COOKIE)
    return resp


# ── Recuperação de acesso ─────────────────────────────────────────────────────

@bp.route("/acesso")
def acesso():
    """Página para recuperar acesso em outro dispositivo usando o e-mail de compra."""
    if security.is_premium(request):
        return redirect(url_for("analysis.index"))
    return render_template("acesso.html")


@bp.route("/acesso/verificar", methods=["POST"])
@rate_limit("ACESSO_RATE_LIMIT", "5 per hour")
def acesso_verificar():
    """Envia, para o e-mail da compra, um link para (re)definir a senha e entrar.

    Saber o e-mail de um cliente não pode bastar para acessar a conta dele:
    o acesso só é liberado pelo link, que prova a posse da caixa de e-mail.
    A resposta é a mesma exista ou não um plano, para não revelar quem é cliente.
    """
    email, erro = validar_email_cadastro(request.form.get("email", ""), verificar_dns=False)
    if erro:
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("auth.acesso"))

    base_url = accounts.app_base_url()
    if not mailer.is_configured() or not base_url:
        logger.error("Recuperação de acesso indisponível: configure SMTP_HOST/SMTP_FROM e APP_BASE_URL.")
        flash(
            "A recuperação por e-mail está indisponível no momento. Se você já tem senha, faça login; "
            "caso contrário, fale com o suporte.",
            "error",
        )
        return redirect(url_for("auth.acesso"))

    if accounts.find_active_plan(email):
        link = f"{base_url}{url_for('auth.login_definir_senha', t=security.generate_setup_token(email))}"
        mailer.send_email(
            to=email,
            subject="Three Health — seu link de acesso",
            body=(
                "Olá,\n\n"
                "Recebemos um pedido de acesso à sua conta Three Health. Para definir sua senha "
                f"e entrar, abra o link abaixo (válido por 24 horas e uma única vez):\n\n{link}\n\n"
                "Se você não fez este pedido, ignore este e-mail — sua conta continua protegida."
            ),
        )

    flash(ACESSO_LINK_ENVIADO, "success")
    return redirect(url_for("auth.acesso"))


@bp.route("/premium/logout")
def premium_logout():
    """Remove o cookie premium (logout)."""
    resp = make_response(redirect(url_for("auth.login")))
    resp.delete_cookie(security.PREMIUM_COOKIE)
    return resp


# ── Login com e-mail e senha ──────────────────────────────────────────────────

@bp.route("/login", methods=["GET", "POST"])
@rate_limit("LOGIN_RATE_LIMIT", "10 per 15 minutes", methods=["POST"])
def login():
    """Tela de login para clientes com senha cadastrada."""
    if security.is_premium(request):
        return redirect(url_for("analysis.index"))
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    senha = request.form.get("senha", "")
    if not email or not senha:
        flash("Preencha e-mail e senha.", "error")
        return render_template("login.html", email=email)

    # Mesma mensagem para "sem plano", "sem senha" e "senha errada": não revela
    # quem é cliente. Conta sem senha nunca recebe link de definição aqui — só
    # por e-mail (/acesso), que comprova a posse da caixa.
    plan = accounts.find_active_plan(email)
    senha_hash = db.buscar_senha_hash_cliente(email) if plan else None
    if not plan or not senha_hash or not check_password_hash(senha_hash, senha):
        flash(_CREDENCIAIS_INVALIDAS, "error")
        return render_template("login.html", email=email)

    return accounts.start_premium_session(
        make_response(redirect(url_for("analysis.index"))),
        email=email,
        payment_id=plan.payment_id,
        plan_key=plan.plan_key,
        valid_until=plan.valid_until,
    )


@bp.route("/login/definir-senha", methods=["GET", "POST"])
def login_definir_senha():
    """Define ou redefine a senha de acesso de um cliente autenticado por token."""
    token = request.args.get("t", "") or request.form.get("t", "")
    email = security.verify_setup_token(token)
    if not email:
        flash("Link inválido ou expirado. Faça login ou recupere o acesso para tentar novamente.", "error")
        return redirect(url_for("auth.login"))

    def _form(message: str | None = None):
        if message:
            flash(message, "error")
        return render_template("definir_senha.html", token=token, email=email)

    if request.method == "GET":
        return _form()

    senha = request.form.get("senha", "")
    if len(senha) < 8:
        return _form("A senha deve ter pelo menos 8 caracteres.")
    if senha != request.form.get("confirmar", ""):
        return _form("As senhas não coincidem.")
    if not db.salvar_senha_cliente(email, generate_password_hash(senha)):
        return _form("Erro ao salvar a senha. Tente novamente.")

    # Troca de senha derruba as sessões abertas em outros aparelhos (ex.: quem
    # conhecia a senha antiga). O cookie emitido abaixo já sai com a nova versão.
    security.revoke_sessions(email)

    plan = accounts.find_active_plan(email)
    if not plan:
        flash("Senha criada! Faça login para acessar.", "success")
        return redirect(url_for("auth.login"))
    return accounts.start_premium_session(
        make_response(redirect(url_for("analysis.index"))),
        email=email,
        payment_id=plan.payment_id,
        plan_key=plan.plan_key,
        valid_until=plan.valid_until,
    )
