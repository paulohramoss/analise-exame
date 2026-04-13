"""
Aplicação Flask para análise de exames médicos com IA (Gemini).
Permite upload de uma ou múltiplas imagens de exames e gera laudos comparativos.
"""

import base64
import hashlib
import os
import sys
import tempfile
import time
import uuid
import subprocess
from functools import wraps
from pathlib import Path
from flask import Flask, g, render_template, request, jsonify, redirect, url_for, flash, make_response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from werkzeug.security import generate_password_hash, check_password_hash

from core.analyzer import analyze_exam
from core import asaas, db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")

# ── Planos disponíveis ────────────────────────────────────────────────────────
PLANS = {
    "mensal": {
        "name": "Three Health – Plano Mensal",
        "price": 60.00,
        "months": 1,
        "cookie_max_age": 30 * 24 * 3600,
    },
    "semestral": {
        "name": "Three Health – Plano Semestral",
        "price": 300.00,
        "months": 6,
        "cookie_max_age": 183 * 24 * 3600,
    },
    "anual": {
        "name": "Three Health – Plano Anual",
        "price": 418.00,
        "months": 12,
        "cookie_max_age": 365 * 24 * 3600,
    },
}
DEFAULT_PLAN = "anual"
PREMIUM_COOKIE = "threehealth_premium"
_COOKIE_SALT = "premium-access-v1"
_ADMIN_COOKIE = "threehealth_admin"
_ADMIN_SALT = "admin-access-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.secret_key)


# ── Token de setup de senha (válido por 24h) ──────────────────────────────────
_SETUP_SALT = "password-setup-v1"


def generate_setup_token(email: str) -> str:
    return _serializer().dumps({"email": email}, salt=_SETUP_SALT)


def verify_setup_token(token: str) -> str | None:
    """Retorna o e-mail contido no token ou None se inválido/expirado."""
    try:
        data = _serializer().loads(token, salt=_SETUP_SALT, max_age=24 * 3600)
        return data.get("email")
    except (BadSignature, SignatureExpired):
        return None


def generate_premium_token(payment_id: str, email: str, cookie_max_age: int = 365 * 24 * 3600) -> str:
    expires_at = int(time.time()) + cookie_max_age
    return _serializer().dumps({"pid": payment_id, "email": email, "exp": expires_at}, salt=_COOKIE_SALT)


def verify_premium_token(token: str) -> dict | None:
    try:
        # Use a generous max_age; actual expiry is enforced via embedded 'exp' field
        data = _serializer().loads(token, salt=_COOKIE_SALT, max_age=10 * 365 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return None
    if "exp" in data:
        if data["exp"] < time.time():
            return None
    else:
        # Legacy tokens (no exp field): enforce original 1-year limit
        try:
            _serializer().loads(token, salt=_COOKIE_SALT, max_age=365 * 24 * 3600)
        except (BadSignature, SignatureExpired):
            return None
    return data


def _is_admin(req) -> bool:
    """Verifica cookie de acesso admin (para testes internos)."""
    token = req.cookies.get(_ADMIN_COOKIE)
    if not token:
        return False
    try:
        _serializer().loads(token, salt=_ADMIN_SALT, max_age=30 * 24 * 3600)
        return True
    except (BadSignature, SignatureExpired):
        return False


def is_premium(req) -> bool:
    # Acesso admin também passa pela verificação de premium
    if _is_admin(req):
        return True
    token = req.cookies.get(PREMIUM_COOKIE)
    if not token:
        return False
    return verify_premium_token(token) is not None


def premium_required(f):
    """Decorator: redireciona para / se não for usuário premium."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_premium(request):
            # Requisições de streaming retornam erro SSE
            if request.headers.get("Accept") == "text/event-stream":
                return Response(
                    'data: {"type":"error","message":"Acesso não autorizado. Contrate um plano para continuar."}\n\n',
                    content_type="text/event-stream",
                )
            # Demais requisições: flash + redireciona para a homepage
            from markupsafe import Markup
            flash(Markup(
                'Para analisar exames, <a href="/planos" style="color:#1d4ed8;font-weight:700;">contrate um plano</a>.'
            ), "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


# Versão do build
def _get_build_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).parent,
        ).decode().strip()
    except Exception:
        return os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7] or "dev"


BUILD_VERSION = _get_build_version()
app.jinja_env.globals["BUILD_VERSION"] = BUILD_VERSION


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_ip() -> str | None:
    """Retorna o IP real do cliente (considera proxy/Vercel)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.remote_addr or None


def _detect_modalidade(user_description: str) -> str | None:
    """Detecta a modalidade de imagem pela descrição do usuário."""
    desc = (user_description or "").lower()
    if any(k in desc for k in ["ressonância", "ressonancia", " rm ", "rm:", "mri", "ressonância magnética"]):
        return "RM"
    if any(k in desc for k in ["raio-x", "raio x", "radiografia", " rx ", "rx:", "x-ray"]):
        return "RX"
    if any(k in desc for k in ["tomografia", " tc ", "tc:", "ct scan"]):
        return "TC"
    if any(k in desc for k in ["ultrassom", "ultrassonografia", " us ", "us:", "ultrasound"]):
        return "US"
    return None


# ── Hooks de request ──────────────────────────────────────────────────────────

@app.before_request
def _before():
    g.t0 = time.time()
    g.analise_id = None
    g.cliente_id = None
    g.modo = None


@app.after_request
def set_cache_headers(response):
    """Impede cache de páginas HTML e registra log de acesso no banco."""
    if "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    # Log de acesso (exclui assets estáticos para não poluir o banco)
    if not request.path.startswith("/static"):
        tempo_ms = int((time.time() - g.get("t0", time.time())) * 1000)
        db.salvar_log(
            endpoint=request.path,
            metodo=request.method,
            status_code=response.status_code,
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            modo=g.get("modo"),
            tempo_ms=tempo_ms,
            cliente_id=g.get("cliente_id"),
            analise_id=g.get("analise_id"),
        )

    return response


# ── Upload ────────────────────────────────────────────────────────────────────

UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "analise_exame_uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "dcm"}
MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20MB
MAX_IMAGES_PER_ANALYSIS = 5

app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")


def get_model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def save_upload_file(file) -> tuple[Path, str, str] | None:
    """
    Salva um arquivo de upload em disco.
    Retorna (filepath, image_b64, image_mime) ou None se inválido.
    """
    if not file or file.filename == "" or not allowed_file(file.filename):
        return None

    original_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    filepath = UPLOAD_FOLDER / unique_name
    file.save(str(filepath))

    ext = filepath.suffix.lower().lstrip(".")
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp", "gif": "image/gif",
    }
    image_mime = mime_map.get(ext, "image/jpeg")

    with open(str(filepath), "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    return filepath, image_b64, image_mime


# ── Acesso admin (testes internos) ───────────────────────────────────────────

@app.route("/admin/entrar")
def admin_entrar():
    """
    Concede acesso de teste via cookie admin.
    Protegido pela variável de ambiente ADMIN_KEY.
    Uso: /admin/entrar?key=SUA_ADMIN_KEY
    Para revogar: /admin/sair
    """
    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    if not admin_key:
        return "ADMIN_KEY não configurada no servidor.", 403

    provided = request.args.get("key", "").strip()
    if not provided or provided != admin_key:
        return "Chave inválida.", 403

    token = _serializer().dumps({"admin": True}, salt=_ADMIN_SALT)
    resp = make_response(redirect(url_for("index")))
    resp.set_cookie(
        _ADMIN_COOKIE,
        token,
        max_age=30 * 24 * 3600,   # 30 dias
        httponly=True,
        samesite="Lax",
        secure=not app.debug,
    )
    return resp


@app.route("/admin/sair")
def admin_sair():
    """Remove o cookie de acesso admin."""
    resp = make_response(redirect(url_for("index")))
    resp.delete_cookie(_ADMIN_COOKIE)
    return resp


# ── Rotas principais ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
@premium_required
def analyze():
    """Endpoint para receber e analisar o(s) exame(s) médico(s)."""
    g.modo = "premium"
    t0 = time.time()

    api_key = get_api_key()
    if not api_key:
        flash("Erro: GEMINI_API_KEY não configurada. Adicione a variável de ambiente no painel do Vercel (Settings → Environment Variables).", "error")
        return redirect(url_for("index"))

    # Suporta múltiplos arquivos via campo "exam_images[]" ou "exam_images"
    files = request.files.getlist("exam_images") or request.files.getlist("exam_images[]")

    # Compatibilidade com campo legado "exam_image" (singular)
    if not files or all(f.filename == "" for f in files):
        legacy = request.files.get("exam_image")
        if legacy and legacy.filename != "":
            files = [legacy]
        else:
            flash("Nenhuma imagem enviada.", "error")
            return redirect(url_for("index"))

    # Limita ao máximo de imagens permitidas
    files = [f for f in files if f.filename != ""][:MAX_IMAGES_PER_ANALYSIS]

    if not files:
        flash("Nenhuma imagem válida enviada.", "error")
        return redirect(url_for("index"))

    user_description = request.form.get("description", "").strip()

    filepaths = []
    images_data = []  # Lista de {"b64": str, "mime": str} para exibição no resultado

    for file in files:
        result = save_upload_file(file)
        if result is None:
            continue
        filepath, image_b64, image_mime = result
        filepaths.append(str(filepath))
        images_data.append({"b64": image_b64, "mime": image_mime})

    if not filepaths:
        flash(f"Formato de arquivo não suportado. Use: {', '.join(ALLOWED_EXTENSIONS)}", "error")
        return redirect(url_for("index"))

    try:
        result = analyze_exam(
            exam_image_paths=filepaths,
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )

        # Persiste análise e imagens no banco
        analise_id = db.salvar_analise(
            tipo_exame=result["exam_type"],
            analise_completa=result["analysis"],
            modelo_ia=result["model_used"],
            referencias_usadas=result["references_used"],
            num_imagens=result["num_images"],
            modo="premium",
            descricao_usuario=user_description,
            modalidade=_detect_modalidade(user_description),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            tempo_ms=int((time.time() - t0) * 1000),
            cliente_id=g.get("cliente_id"),
        )
        if analise_id:
            g.analise_id = analise_id
            for i, img_data in enumerate(images_data, 1):
                raw = base64.b64decode(img_data["b64"])
                db.salvar_imagem_exame(
                    analise_id=analise_id,
                    mime_type=img_data["mime"],
                    tamanho_bytes=len(raw),
                    hash_md5=hashlib.md5(raw).hexdigest(),
                    ordem=i,
                )

        return render_template(
            "result.html",
            analysis=result["analysis"],
            exam_type=result["exam_type"].replace("_", " ").title(),
            references_used=result["references_used"],
            model_used=result["model_used"],
            images=images_data,
            num_images=result["num_images"],
            user_description=user_description,
        )

    except Exception as e:
        error_msg = str(e)
        print(f"[ERRO ANÁLISE] {type(e).__name__}: {error_msg}", file=sys.stderr)
        error_lower = error_msg.lower()
        if "api key not valid" in error_lower or "invalid api key" in error_lower or "api_key_invalid" in error_lower:
            flash("Erro de autenticação: GEMINI_API_KEY inválida. Verifique a chave no painel do Vercel.", "error")
        elif "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
            flash("Limite de requisições atingido. Aguarde alguns instantes e tente novamente.", "error")
        elif "503" in error_lower or "unavailable" in error_lower or "overloaded" in error_lower:
            flash("O serviço de IA está temporariamente sobrecarregado. Aguarde alguns segundos e tente novamente.", "error")
        elif "404" in error_lower or "not found" in error_lower:
            flash("Modelo de IA temporariamente indisponível. Tente novamente em instantes.", "error")
        else:
            print(f"[ERRO INESPERADO] {error_msg}", file=sys.stderr)
            flash("Não foi possível processar o exame. Tente novamente.", "error")
        return redirect(url_for("index"))

    finally:
        for fp in filepaths:
            try:
                Path(fp).unlink()
            except Exception:
                pass


@app.route("/trial")
def trial():
    """Redireciona para a página principal."""
    return redirect(url_for("index"), 301)


@app.route("/trial/analyze", methods=["POST"])
def trial_analyze():
    """Endpoint para análise no modo de teste gratuito (retorna JSON, aceita apenas 1 imagem)."""
    g.modo = "trial"
    t0 = time.time()

    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "Serviço temporariamente indisponível. Tente novamente mais tarde."}), 503

    if "exam_image" not in request.files:
        return jsonify({"error": "Nenhuma imagem enviada."}), 400

    file = request.files["exam_image"]
    if file.filename == "":
        return jsonify({"error": "Nenhum arquivo selecionado."}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"Formato não suportado. Use: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    user_description = request.form.get("description", "").strip()

    result_save = save_upload_file(file)
    if result_save is None:
        return jsonify({"error": "Erro ao processar o arquivo enviado."}), 400

    filepath, image_b64, image_mime = result_save

    try:
        result = analyze_exam(
            exam_image_paths=[str(filepath)],
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )

        # Persiste análise no banco
        analise_id = db.salvar_analise(
            tipo_exame=result["exam_type"],
            analise_completa=result["analysis"],
            modelo_ia=result["model_used"],
            referencias_usadas=result["references_used"],
            num_imagens=1,
            modo="trial",
            descricao_usuario=user_description,
            modalidade=_detect_modalidade(user_description),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            tempo_ms=int((time.time() - t0) * 1000),
        )
        if analise_id:
            g.analise_id = analise_id
            raw = base64.b64decode(image_b64)
            db.salvar_imagem_exame(
                analise_id=analise_id,
                mime_type=image_mime,
                tamanho_bytes=len(raw),
                hash_md5=hashlib.md5(raw).hexdigest(),
            )

        return jsonify({
            "analysis": result["analysis"],
            "exam_type": result["exam_type"].replace("_", " ").title(),
            "references_used": result["references_used"],
            "model_used": result["model_used"],
            "image_b64": image_b64,
            "image_mime": image_mime,
        }), 200

    except Exception as e:
        error_msg = str(e)
        print(f"[ERRO TRIAL ANÁLISE] {type(e).__name__}: {error_msg}", file=sys.stderr)
        error_lower = error_msg.lower()
        if "api key not valid" in error_lower or "invalid api key" in error_lower:
            return jsonify({"error": "Serviço temporariamente indisponível."}), 503
        elif "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
            return jsonify({"error": "Limite de requisições atingido. Tente novamente em alguns instantes."}), 429
        elif "503" in error_lower or "unavailable" in error_lower or "overloaded" in error_lower:
            return jsonify({"error": "Serviço de IA sobrecarregado. Aguarde alguns segundos e tente novamente."}), 503
        elif "404" in error_lower or "not found" in error_lower:
            return jsonify({"error": "Serviço de IA temporariamente indisponível. Tente novamente."}), 503
        return jsonify({"error": "Não foi possível processar o exame. Tente novamente."}), 500

    finally:
        try:
            filepath.unlink()
        except Exception:
            pass


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Endpoint REST para integração programática. Suporta múltiplas imagens."""
    g.modo = "api"
    t0 = time.time()

    api_key = request.headers.get("X-API-Key") or get_api_key()
    if not api_key:
        return jsonify({"error": "API key não fornecida"}), 401

    # Suporta múltiplos arquivos via exam_images ou exam_images[]
    files = request.files.getlist("exam_images") or request.files.getlist("exam_images[]")
    if not files or all(f.filename == "" for f in files):
        # Compatibilidade com campo legado
        legacy = request.files.get("exam_image")
        if legacy:
            files = [legacy]
        else:
            return jsonify({"error": "Nenhuma imagem enviada"}), 400

    files = [f for f in files if f and f.filename != "" and allowed_file(f.filename)]
    files = files[:MAX_IMAGES_PER_ANALYSIS]

    if not files:
        return jsonify({"error": "Formato de arquivo não suportado"}), 400

    user_description = request.form.get("description", "")

    filepaths = []
    for file in files:
        original_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{original_name}"
        filepath = UPLOAD_FOLDER / unique_name
        file.save(str(filepath))
        filepaths.append(str(filepath))

    try:
        result = analyze_exam(
            exam_image_paths=filepaths,
            api_key=api_key,
            user_description=user_description,
            model_name=get_model_name(),
        )

        # Persiste análise no banco
        analise_id = db.salvar_analise(
            tipo_exame=result["exam_type"],
            analise_completa=result["analysis"],
            modelo_ia=result["model_used"],
            referencias_usadas=result["references_used"],
            num_imagens=result["num_images"],
            modo="api",
            descricao_usuario=user_description,
            modalidade=_detect_modalidade(user_description),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            tempo_ms=int((time.time() - t0) * 1000),
        )
        if analise_id:
            g.analise_id = analise_id

        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

    finally:
        for fp in filepaths:
            try:
                Path(fp).unlink()
            except Exception:
                pass


# ── Planos ───────────────────────────────────────────────────────────────────

@app.route("/planos")
def planos():
    """Página pública de comparação de planos — usada na LP e no site."""
    return render_template("planos.html", plans=PLANS)


# ── Checkout / Pagamento ──────────────────────────────────────────────────────

@app.route("/checkout")
def checkout():
    """Página de contratação com formulário de nome + e-mail."""
    if is_premium(request):
        return redirect(url_for("index"))
    plan = request.args.get("plan", DEFAULT_PLAN)
    if plan not in PLANS:
        plan = DEFAULT_PLAN
    return render_template("checkout.html", plans=PLANS, default_plan=plan)


@app.route("/checkout/pay", methods=["POST"])
def checkout_pay():
    """Cria cliente + cobrança no Asaas. Retorna JSON com dados de pagamento inline."""
    name         = request.form.get("name", "").strip()
    email        = request.form.get("email", "").strip()
    cpf_cnpj     = request.form.get("cpf_cnpj", "").strip()
    plan_key     = request.form.get("plan", DEFAULT_PLAN)
    billing_type = request.form.get("billing_type", "PIX").upper()

    # Senha de acesso definida no checkout
    senha_acesso   = request.form.get("senha_acesso", "").strip()
    senha_confirmar = request.form.get("senha_confirmar", "").strip()

    # Campos de cartão (só usados quando billing_type == CREDIT_CARD)
    card_number       = request.form.get("card_number", "").replace(" ", "").strip()
    card_holder       = request.form.get("card_holder", name).strip() or name
    card_expiry       = request.form.get("card_expiry", "").strip()   # formato MM/AA
    card_cvv          = request.form.get("card_cvv", "").strip()
    card_postal_code  = request.form.get("card_postal_code", "").replace("-", "").strip()
    card_address_num  = request.form.get("card_address_number", "").strip()
    card_phone        = request.form.get("card_phone", "").strip()

    if plan_key not in PLANS:
        plan_key = DEFAULT_PLAN
    plan = PLANS[plan_key]

    if billing_type not in asaas.BILLING_TYPES:
        billing_type = "PIX"

    # ── Validações básicas ────────────────────────────────────────────────────
    if not name or not email or "@" not in email:
        return jsonify({"success": False, "error": "Preencha nome e e-mail válidos."}), 400

    if not cpf_cnpj:
        return jsonify({"success": False, "error": "Preencha o CPF ou CNPJ."}), 400

    if not senha_acesso or len(senha_acesso) < 8:
        return jsonify({"success": False, "error": "A senha deve ter pelo menos 8 caracteres."}), 400

    if senha_acesso != senha_confirmar:
        return jsonify({"success": False, "error": "As senhas não coincidem."}), 400

    if billing_type == "CREDIT_CARD":
        if not card_number or len(card_number) < 13:
            return jsonify({"success": False, "error": "Número do cartão inválido."}), 400
        if not card_expiry or "/" not in card_expiry:
            return jsonify({"success": False, "error": "Data de validade inválida (use MM/AA)."}), 400
        if not card_cvv:
            return jsonify({"success": False, "error": "CVV inválido."}), 400
        if not card_postal_code:
            return jsonify({"success": False, "error": "Informe o CEP do titular do cartão."}), 400
        if not card_address_num:
            return jsonify({"success": False, "error": "Informe o número do endereço do titular."}), 400

    if not os.environ.get("ASAAS_API_KEY", ""):
        return jsonify({"success": False, "error": "Serviço de pagamento indisponível. Tente mais tarde."}), 503

    external_reference = f"{email}|{plan_key}"

    # Monta dados do cartão se necessário
    credit_card = None
    credit_card_holder_info = None
    if billing_type == "CREDIT_CARD":
        expiry_parts = card_expiry.split("/")
        exp_month = expiry_parts[0].zfill(2)
        exp_year_raw = expiry_parts[1].strip() if len(expiry_parts) > 1 else ""
        exp_year = f"20{exp_year_raw}" if len(exp_year_raw) == 2 else exp_year_raw

        credit_card = {
            "holderName": card_holder,
            "number": card_number,
            "expiryMonth": exp_month,
            "expiryYear": exp_year,
            "ccv": card_cvv,
        }
        cpf_digits = "".join(c for c in cpf_cnpj if c.isdigit())
        phone_digits = "".join(c for c in card_phone if c.isdigit())
        credit_card_holder_info = {
            "name": name,
            "email": email,
            "cpfCnpj": cpf_digits,
            "postalCode": card_postal_code,
            "addressNumber": card_address_num,
            "phone": phone_digits or None,
        }

    # ── Criação no Asaas ──────────────────────────────────────────────────────
    try:
        customer = asaas.create_customer(name=name, email=email, cpf_cnpj=cpf_cnpj)
        payment = asaas.create_payment(
            customer_id=customer["id"],
            value=plan["price"],
            description=plan["name"],
            external_reference=external_reference,
            billing_type=billing_type,
            credit_card=credit_card,
            credit_card_holder_info=credit_card_holder_info,
        )
    except Exception as e:
        err_str = str(e)
        print(f"[ASAAS] Erro ao criar cobrança: {type(e).__name__}: {err_str}", file=sys.stderr)
        if "401" in err_str or "Unauthorized" in err_str or "unauthorized" in err_str.lower():
            msg = "Chave da API Asaas inválida. Verifique ASAAS_API_KEY."
        elif "403" in err_str:
            msg = "Sem permissão na API Asaas."
        elif "ConnectionError" in err_str or "Timeout" in err_str or "timeout" in err_str.lower():
            msg = "Não foi possível conectar ao Asaas. Tente novamente."
        else:
            msg = f"Erro ao gerar cobrança: {err_str[:200]}"
        return jsonify({"success": False, "error": msg}), 502

    payment_id  = payment.get("id", "")
    invoice_url = payment.get("invoiceUrl") or payment.get("bankSlipUrl") or ""

    if not payment_id:
        return jsonify({"success": False, "error": "Erro ao criar cobrança. Tente novamente."}), 502

    # ── Persistência ──────────────────────────────────────────────────────────
    cliente_id = db.upsert_cliente(name, email, cpf_cnpj, customer.get("id", ""))
    g.cliente_id = cliente_id

    # Salva a senha de acesso definida durante o checkout
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

    cookie_max_age = plan["cookie_max_age"]

    # ── Resposta por tipo de pagamento ────────────────────────────────────────
    if billing_type == "PIX":
        try:
            pix = asaas.get_pix_qr_code(payment_id)
        except Exception as e:
            print(f"[ASAAS] Erro QR PIX: {e}", file=sys.stderr)
            pix = {}
        return jsonify({
            "success": True,
            "billing_type": "PIX",
            "payment_id": payment_id,
            "pix_qr_image": pix.get("encodedImage", ""),
            "pix_copy_paste": pix.get("payload", ""),
        })

    if billing_type == "BOLETO":
        try:
            boleto = asaas.get_boleto_identification(payment_id)
        except Exception as e:
            print(f"[ASAAS] Erro boleto ID: {e}", file=sys.stderr)
            boleto = {}
        return jsonify({
            "success": True,
            "billing_type": "BOLETO",
            "payment_id": payment_id,
            "boleto_barcode": boleto.get("identificationField", ""),
            "boleto_url": invoice_url,
        })

    if billing_type == "CREDIT_CARD":
        status = payment.get("status", "")
        confirmed = status in asaas.CONFIRMED_STATUSES
        payload: dict = {
            "success": True,
            "billing_type": "CREDIT_CARD",
            "payment_id": payment_id,
            "confirmed": confirmed,
        }
        # setup_token só necessário se a senha ainda não foi definida (checkout sempre define)
        if confirmed:
            senha_hash_atual = db.buscar_senha_hash_cliente(email)
            if not senha_hash_atual:
                payload["setup_token"] = generate_setup_token(email)
        resp = jsonify(payload)
        if confirmed:
            db.atualizar_status_pagamento(payment_id, status)
            pagamento_db_id = db.buscar_pagamento_id(payment_id)
            db.salvar_sessao(
                cliente_id=cliente_id,
                pagamento_id=pagamento_db_id,
                ip_address=_get_ip(),
                user_agent=request.headers.get("User-Agent"),
            )
            token = generate_premium_token(payment_id, email, cookie_max_age)
            resp.set_cookie(
                PREMIUM_COOKIE, token, max_age=cookie_max_age,
                httponly=True, samesite="Lax", secure=not app.debug,
            )
        return resp

    # Fallback genérico
    return jsonify({"success": True, "billing_type": billing_type, "payment_id": payment_id})


@app.route("/checkout/pending")
def checkout_pending():
    """Aguarda confirmação do pagamento com polling automático."""
    payment_id = request.args.get("payment_id", "")
    invoice_url = request.args.get("invoice_url", "")
    if not payment_id:
        return redirect(url_for("checkout"))
    return render_template("checkout_pending.html", payment_id=payment_id, invoice_url=invoice_url)


@app.route("/api/payment/status")
def payment_status():
    """Verifica o status de uma cobrança no Asaas (usado pelo polling do frontend)."""
    payment_id = request.args.get("id", "").strip()
    if not payment_id:
        return jsonify({"confirmed": False, "error": "ID não informado"}), 400

    try:
        confirmed = asaas.is_payment_confirmed(payment_id)
    except Exception as e:
        print(f"[ASAAS] Erro ao verificar pagamento {payment_id}: {e}", file=sys.stderr)
        return jsonify({"confirmed": False, "error": "Erro ao consultar pagamento"}), 502

    if confirmed:
        # Busca externalReference para extrair e-mail e plano
        try:
            payment = asaas.get_payment(payment_id)
            ext_ref = payment.get("externalReference", "")
            if "|" in ext_ref:
                email, plan_key = ext_ref.split("|", 1)
            else:
                email, plan_key = ext_ref, DEFAULT_PLAN
        except Exception:
            email, plan_key = "", DEFAULT_PLAN

        if plan_key not in PLANS:
            plan_key = DEFAULT_PLAN
        cookie_max_age = PLANS[plan_key]["cookie_max_age"]

        token = generate_premium_token(payment_id, email, cookie_max_age)
        # setup_token só é necessário se o cliente ainda não tem senha (fluxo legado /acesso)
        senha_hash_atual = db.buscar_senha_hash_cliente(email) if email else None
        setup_token = "" if senha_hash_atual else (generate_setup_token(email) if email else "")
        resp = jsonify({"confirmed": True, "setup_token": setup_token})
        resp.set_cookie(
            PREMIUM_COOKIE,
            token,
            max_age=cookie_max_age,
            httponly=True,
            samesite="Lax",
            secure=not app.debug,
        )

        # Atualiza status e registra sessão premium no banco
        db.atualizar_status_pagamento(payment_id, "CONFIRMED")
        cliente_id = db.buscar_cliente_id_por_email(email) if email else None
        pagamento_id = db.buscar_pagamento_id(payment_id)
        db.salvar_sessao(
            cliente_id=cliente_id,
            pagamento_id=pagamento_id,
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
        )
        g.cliente_id = cliente_id

        return resp

    return jsonify({"confirmed": False})


@app.route("/webhook/asaas", methods=["POST"])
def webhook_asaas():
    """
    Recebe notificações de pagamento do Asaas.
    Persiste o evento e atualiza o status do pagamento no banco.
    """
    # Valida token de autenticação do webhook (configure no painel Asaas)
    webhook_token = os.environ.get("ASAAS_WEBHOOK_TOKEN", "")
    if webhook_token:
        received_token = request.headers.get("asaas-access-token", "")
        if received_token != webhook_token:
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    event = data.get("event", "")
    payment = data.get("payment", {})
    payment_id = payment.get("id", "")
    status = payment.get("status", "")

    print(f"[ASAAS WEBHOOK] event={event} payment_id={payment_id} status={status}", file=sys.stderr)

    # Persiste webhook e atualiza status do pagamento
    db.salvar_webhook(
        evento=event,
        asaas_payment_id=payment_id,
        status_pagamento=status,
        payload=data,
    )
    if payment_id and status:
        db.atualizar_status_pagamento(payment_id, status, payload=data)

    return jsonify({"received": True}), 200


@app.route("/acesso")
def acesso():
    """Página para recuperar acesso em outro dispositivo usando o e-mail de compra."""
    if is_premium(request):
        return redirect(url_for("index"))
    return render_template("acesso.html")


@app.route("/acesso/verificar", methods=["POST"])
def acesso_verificar():
    """Verifica se o e-mail tem pagamento confirmado e emite o cookie premium."""
    email = request.form.get("email", "").strip().lower()
    if not email or "@" not in email:
        flash("Informe um e-mail válido.", "error")
        return redirect(url_for("acesso"))

    result = db.buscar_pagamento_confirmado_por_email(email)
    if not result:
        flash("Nenhum pagamento confirmado encontrado para este e-mail.", "error")
        return redirect(url_for("acesso"))

    payment_id, ext_ref = result
    if ext_ref and "|" in ext_ref:
        _, plan_key = ext_ref.split("|", 1)
    else:
        plan_key = DEFAULT_PLAN
    if plan_key not in PLANS:
        plan_key = DEFAULT_PLAN
    cookie_max_age = PLANS[plan_key]["cookie_max_age"]

    premium_token = generate_premium_token(payment_id, email, cookie_max_age)

    # Se o cliente ainda não tem senha, redireciona para criar uma
    senha_hash = db.buscar_senha_hash_cliente(email)
    if not senha_hash:
        setup_token = generate_setup_token(email)
        resp = make_response(redirect(url_for("login_definir_senha", t=setup_token)))
    else:
        resp = make_response(redirect(url_for("index")))

    resp.set_cookie(
        PREMIUM_COOKIE,
        premium_token,
        max_age=cookie_max_age,
        httponly=True,
        samesite="Lax",
        secure=not app.debug,
    )

    cliente_id = db.buscar_cliente_id_por_email(email)
    pagamento_db_id = db.buscar_pagamento_id(payment_id)
    db.salvar_sessao(
        cliente_id=cliente_id,
        pagamento_id=pagamento_db_id,
        ip_address=_get_ip(),
        user_agent=request.headers.get("User-Agent"),
    )
    g.cliente_id = cliente_id

    return resp


@app.route("/premium/logout")
def premium_logout():
    """Remove o cookie premium (logout)."""
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie(PREMIUM_COOKIE)
    return resp


# ── Login com e-mail e senha ───────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    """Tela de login para clientes com senha cadastrada."""
    if is_premium(request):
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    senha = request.form.get("senha", "")

    if not email or not senha:
        flash("Preencha e-mail e senha.", "error")
        return render_template("login.html", email=email)

    # 1. Verifica se há plano ativo para o e-mail
    result = db.buscar_pagamento_confirmado_por_email(email)
    if not result:
        flash("E-mail não encontrado ou sem plano ativo.", "error")
        return render_template("login.html", email=email)

    payment_id, ext_ref = result

    # 2. Verifica se senha foi definida
    senha_hash = db.buscar_senha_hash_cliente(email)
    if not senha_hash:
        # Sem senha: oferece fluxo de criação
        setup_token = generate_setup_token(email)
        flash("Você ainda não criou uma senha. Defina uma agora para acessar por aqui.", "info")
        return redirect(url_for("login_definir_senha", t=setup_token))

    # 3. Verifica a senha
    if not check_password_hash(senha_hash, senha):
        flash("Senha incorreta. Tente novamente.", "error")
        return render_template("login.html", email=email)

    # 4. Tudo certo — emite cookie premium
    if ext_ref and "|" in ext_ref:
        _, plan_key = ext_ref.split("|", 1)
    else:
        plan_key = DEFAULT_PLAN
    if plan_key not in PLANS:
        plan_key = DEFAULT_PLAN
    cookie_max_age = PLANS[plan_key]["cookie_max_age"]

    premium_token = generate_premium_token(payment_id, email, cookie_max_age)
    resp = make_response(redirect(url_for("index")))
    resp.set_cookie(
        PREMIUM_COOKIE,
        premium_token,
        max_age=cookie_max_age,
        httponly=True,
        samesite="Lax",
        secure=not app.debug,
    )

    cliente_id = db.buscar_cliente_id_por_email(email)
    pagamento_db_id = db.buscar_pagamento_id(payment_id)
    db.salvar_sessao(
        cliente_id=cliente_id,
        pagamento_id=pagamento_db_id,
        ip_address=_get_ip(),
        user_agent=request.headers.get("User-Agent"),
    )
    g.cliente_id = cliente_id

    return resp


@app.route("/login/definir-senha", methods=["GET", "POST"])
def login_definir_senha():
    """Define ou redefine a senha de acesso de um cliente autenticado por token."""
    token = request.args.get("t", "") or request.form.get("t", "")
    email = verify_setup_token(token)

    if not email:
        flash("Link inválido ou expirado. Faça login ou recupere o acesso para tentar novamente.", "error")
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("definir_senha.html", token=token, email=email)

    senha = request.form.get("senha", "")
    confirmar = request.form.get("confirmar", "")

    if len(senha) < 8:
        flash("A senha deve ter pelo menos 8 caracteres.", "error")
        return render_template("definir_senha.html", token=token, email=email)

    if senha != confirmar:
        flash("As senhas não coincidem.", "error")
        return render_template("definir_senha.html", token=token, email=email)

    senha_hash = generate_password_hash(senha)
    ok = db.salvar_senha_cliente(email, senha_hash)
    if not ok:
        flash("Erro ao salvar a senha. Tente novamente.", "error")
        return render_template("definir_senha.html", token=token, email=email)

    # Emite cookie premium se o cliente tiver plano ativo
    result = db.buscar_pagamento_confirmado_por_email(email)
    if result:
        payment_id, ext_ref = result
        if ext_ref and "|" in ext_ref:
            _, plan_key = ext_ref.split("|", 1)
        else:
            plan_key = DEFAULT_PLAN
        if plan_key not in PLANS:
            plan_key = DEFAULT_PLAN
        cookie_max_age = PLANS[plan_key]["cookie_max_age"]
        premium_token = generate_premium_token(payment_id, email, cookie_max_age)
        resp = make_response(redirect(url_for("index")))
        resp.set_cookie(
            PREMIUM_COOKIE,
            premium_token,
            max_age=cookie_max_age,
            httponly=True,
            samesite="Lax",
            secure=not app.debug,
        )
        cliente_id = db.buscar_cliente_id_por_email(email)
        db.salvar_sessao(
            cliente_id=cliente_id,
            pagamento_id=db.buscar_pagamento_id(payment_id),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
        )
        g.cliente_id = cliente_id
    else:
        resp = make_response(redirect(url_for("login")))
        flash("Senha criada! Faça login para acessar.", "success")

    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
