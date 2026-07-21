"""
Aplicação Flask para análise de exames médicos com IA (Gemini).
Permite upload de uma ou múltiplas imagens de exames e gera laudos comparativos.
"""

import base64
import hmac
import hashlib
import importlib
import logging
import os
import secrets
import tempfile
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, g, render_template, request, jsonify, redirect, url_for, flash, make_response, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from PIL import Image

from werkzeug.security import generate_password_hash, check_password_hash

try:
    _flask_limiter = importlib.import_module("flask_limiter")
    _flask_limiter_errors = importlib.import_module("flask_limiter.errors")
    Limiter = getattr(_flask_limiter, "Limiter")
    RateLimitExceeded = getattr(_flask_limiter_errors, "RateLimitExceeded")
except ImportError:  # pragma: no cover - dependency is installed in production via requirements.txt
    Limiter = None
    RateLimitExceeded = None

try:
    _sentry_sdk = importlib.import_module("sentry_sdk")
    _SentryFlaskIntegration = getattr(importlib.import_module("sentry_sdk.integrations.flask"), "FlaskIntegration")
    _SentryLoggingIntegration = getattr(importlib.import_module("sentry_sdk.integrations.logging"), "LoggingIntegration")
except ImportError:  # pragma: no cover - dependência instalada em produção via requirements.txt
    _sentry_sdk = None

from core.analyzer import analyze_exam_from_bytes
from core import asaas, cost_tracking, db, jobs
from core.build_info import get_build_version
from core.logging_config import configure_logging, new_trace_id

load_dotenv()
configure_logging()

logger = logging.getLogger(__name__)

DEFAULT_FLASK_SECRET_KEY = "dev-secret-key-change-in-prod"
# Gerada uma vez por processo (nunca hardcoded/commitada) — usada apenas quando
# ADMIN_KEY não está definida e o runtime é local (ver _is_local_runtime()).
_GENERATED_LOCAL_ADMIN_KEY = secrets.token_urlsafe(18)
try:
    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - fallback para runtimes sem base IANA local
    LOCAL_TZ = None


def _is_production_like() -> bool:
    env_values = {
        os.environ.get("VERCEL_ENV", ""),
        os.environ.get("FLASK_ENV", ""),
        os.environ.get("ENV", ""),
        os.environ.get("APP_ENV", ""),
    }
    return any(value.lower() in {"production", "prod"} for value in env_values)


def _is_local_runtime() -> bool:
    return not os.environ.get("VERCEL") and not _is_production_like()


def _get_admin_key() -> str:
    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    if admin_key:
        return admin_key
    if _is_local_runtime():
        return _GENERATED_LOCAL_ADMIN_KEY
    return ""


flask_secret_key = os.environ.get("FLASK_SECRET_KEY", DEFAULT_FLASK_SECRET_KEY)
if _is_production_like() and flask_secret_key == DEFAULT_FLASK_SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY deve ser definido com valor seguro em produção.")

app = Flask(__name__)
app.secret_key = flask_secret_key

if _is_local_runtime() and not os.environ.get("ADMIN_KEY", "").strip():
    logger.warning(
        "ADMIN_KEY não definida — acesso admin local liberado via /admin/entrar?key=%s "
        "(chave temporária, gerada nesta execução; defina ADMIN_KEY no .env para uma chave estável).",
        _GENERATED_LOCAL_ADMIN_KEY,
    )

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


BUILD_VERSION = get_build_version()
app.jinja_env.globals["BUILD_VERSION"] = BUILD_VERSION

_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN and _sentry_sdk:
    _sentry_sdk.init(
        dsn=_SENTRY_DSN,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "").strip() or ("production" if _is_production_like() else "development"),
        release=BUILD_VERSION,
        integrations=[
            _SentryFlaskIntegration(),
            _SentryLoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
    )
    logger.info("Sentry inicializado (release=%s)", BUILD_VERSION)
elif _SENTRY_DSN:
    logger.warning("SENTRY_DSN definido, mas o pacote sentry-sdk não está instalado.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_ip() -> str | None:
    """Retorna o IP real do cliente (considera proxy/Vercel)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.remote_addr or None


def _rate_limit_key() -> str:
    return _get_ip() or "anonymous"


def _secure_cookie() -> bool:
    host = (request.host or "").split(":", 1)[0]
    if host in {"127.0.0.1", "localhost", "::1"}:
        return False
    return request.is_secure or _is_production_like() or bool(os.environ.get("VERCEL"))


limiter = None
if Limiter is not None:
    limiter = Limiter(
        key_func=_rate_limit_key,
        app=app,
        storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
        default_limits=[],
    )


def rate_limit(env_name: str, default: str):
    def decorator(fn):
        if limiter is None:
            return fn
        return limiter.limit(os.environ.get(env_name, default))(fn)
    return decorator


if RateLimitExceeded is not None:
    @app.errorhandler(RateLimitExceeded)
    def _handle_rate_limit(error):
        message = "Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente."
        retry_after = getattr(error, "retry_after", None)

        if request.path.startswith(("/trial", "/api")):
            response = jsonify({"error": message})
            response.status_code = 429
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response

        flash(message, "error")
        response = make_response(redirect(url_for("index")), 429)
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        return response


def _has_privacy_consent() -> bool:
    value = (request.form.get("privacy_consent") or "").strip().lower()
    return value in {"accepted", "on", "true", "1", "yes"}


def _form_text(name: str, max_length: int = 120) -> str:
    value = (request.form.get(name) or "").strip()
    return " ".join(value.split())[:max_length]


def _responsible_info_from_form() -> dict[str, str]:
    return {
        "name": _form_text("responsible_name"),
        "role": _form_text("responsible_role", 80),
        "register": _form_text("responsible_register", 80),
        "organization": _form_text("responsible_organization", 120),
    }


def _responsible_info_from_row(row: dict | None) -> dict[str, str]:
    row = row or {}
    return {
        "name": row.get("responsavel_nome") or "",
        "role": row.get("responsavel_perfil") or "",
        "register": row.get("responsavel_registro") or "",
        "organization": row.get("responsavel_instituicao") or "",
    }


def _format_exam_label(value: str | None) -> str:
    labels = {
        "joelho": "Joelho",
        "coluna": "Coluna Vertebral",
        "ombro": "Ombro",
        "quadril": "Quadril",
        "pe_tornozelo": "Pé e Tornozelo",
        "mao_punho": "Mão e Punho",
        "cotovelo": "Cotovelo",
        "geral": "Região Ortopédica",
    }
    key = (value or "").strip().lower()
    return labels.get(key, (value or "Exame Ortopédico").replace("_", " ").title())


def _format_datetime_br(value) -> str:
    if not value:
        return "Data não informada"
    try:
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
        else:
            dt = value
        if getattr(dt, "tzinfo", None) and LOCAL_TZ:
            dt = dt.astimezone(LOCAL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def _build_consensus_info(model_used: str = "", analysis: str = "") -> dict:
    model_text = model_used or ""
    lower_model = model_text.lower()
    dual = "claude" in lower_model and "consenso" in lower_model
    strong_count = (analysis or "").count("[FORTE]")
    moderate_count = (analysis or "").count("[MODERADO]")

    if dual:
        return {
            "enabled": True,
            "status": "Consenso dual concluído",
            "label": "Gemini + Claude",
            "description": (
                "Dois modelos analisaram o exame de forma independente. A síntese final prioriza achados "
                "convergentes e rebaixa ou descarta achados com evidência insuficiente."
            ),
            "model_a": "Gemini 2.5 Flash",
            "model_b": "Claude Sonnet 4.6",
            "strong_count": strong_count,
            "moderate_count": moderate_count,
        }

    return {
        "enabled": False,
        "status": "Análise por IA concluída",
        "label": model_text or "Modelo principal",
        "description": (
            "Laudo gerado pelo modelo principal configurado no servidor. A revisão clínica do profissional "
            "responsável continua obrigatória."
        ),
        "model_a": model_text or "Modelo principal",
        "model_b": "",
        "strong_count": strong_count,
        "moderate_count": moderate_count,
    }


def _feedback_source_for_role(role: str = "") -> str:
    role_lower = (role or "").lower()
    if "radiologista" in role_lower or "laudista" in role_lower:
        return "radiologista"
    if _is_admin(request):
        return "admin"
    return "medico"


def _split_validation_lines(value: str, limit: int = 12) -> list[str]:
    lines = []
    for line in (value or "").replace(";", "\n").splitlines():
        cleaned = " ".join(line.strip().split())
        if cleaned:
            lines.append(cleaned[:400])
        if len(lines) >= limit:
            break
    return lines


def _score_value(value, default: int = 100) -> int:
    try:
        score = int(value)
    except Exception:
        score = default
    return max(0, min(score, 100))


def _premium_payload_from_request(req) -> dict | None:
    token = req.cookies.get(PREMIUM_COOKIE)
    if not token:
        return None
    return verify_premium_token(token)


app.jinja_env.filters["datetime_br"] = _format_datetime_br
app.jinja_env.filters["exam_label"] = _format_exam_label


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
    g.trace_id = new_trace_id()
    g.analise_id = None
    g.cliente_id = None
    g.user_email = None
    g.is_admin = False
    g.premium_payload = None
    g.modo = None

    if request.path.startswith("/static"):
        return

    g.is_admin = _is_admin(request)
    if g.is_admin:
        return

    payload = _premium_payload_from_request(request)
    g.premium_payload = payload
    if payload and payload.get("email"):
        g.user_email = str(payload.get("email", "")).strip().lower()
        g.cliente_id = db.buscar_cliente_id_por_email(g.user_email)
        if not g.cliente_id:
            g.cliente_id = db.upsert_cliente(g.user_email.split("@")[0], g.user_email)


@app.after_request
def set_cache_headers(response):
    """Impede cache de páginas HTML e registra log de acesso no banco."""
    trace_id = g.get("trace_id")
    if trace_id:
        response.headers["X-Trace-Id"] = trace_id

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


def _hash_sensitive_value(value: str | None) -> str:
    if not value:
        return ""
    seed = f"{app.secret_key}:{value}".encode("utf-8", "ignore")
    return hashlib.sha256(seed).hexdigest()[:24]


def _dicom_value(ds, name: str) -> str:
    value = getattr(ds, name, "")
    if value is None:
        return ""
    return " ".join(str(value).split())[:180]


def _dicom_first_number(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        if isinstance(value, (list, tuple)) or hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            if len(value) == 0:
                return default
            value = value[0]
        return float(value)
    except Exception:
        return default


def _extract_dicom_metadata(ds) -> dict:
    rows = getattr(ds, "Rows", None)
    cols = getattr(ds, "Columns", None)
    metadata = {
        "modality": _dicom_value(ds, "Modality"),
        "body_part": _dicom_value(ds, "BodyPartExamined"),
        "study_description": _dicom_value(ds, "StudyDescription"),
        "series_description": _dicom_value(ds, "SeriesDescription"),
        "protocol_name": _dicom_value(ds, "ProtocolName"),
        "rows": int(rows) if rows else None,
        "columns": int(cols) if cols else None,
        "patient_id_hash": _hash_sensitive_value(_dicom_value(ds, "PatientID")),
        "study_uid_hash": _hash_sensitive_value(_dicom_value(ds, "StudyInstanceUID")),
        "series_uid_hash": _hash_sensitive_value(_dicom_value(ds, "SeriesInstanceUID")),
        "sop_uid_hash": _hash_sensitive_value(_dicom_value(ds, "SOPInstanceUID")),
    }
    return {key: value for key, value in metadata.items() if value not in {"", None}}


def _normalize_dicom_pixels(ds, pixel_array):
    np = importlib.import_module("numpy")
    array = pixel_array

    if array.ndim == 4:
        array = array[0]
    elif array.ndim == 3 and array.shape[-1] not in (3, 4):
        array = array[0]

    if array.ndim == 3 and array.shape[-1] in (3, 4):
        image = array
        if image.dtype != np.uint8:
            low, high = np.percentile(image.astype("float32"), [1, 99])
            if high <= low:
                high = low + 1
            image = np.clip((image.astype("float32") - low) * 255.0 / (high - low), 0, 255)
        return image.astype("uint8")

    image = array.astype("float32")
    slope = _dicom_first_number(getattr(ds, "RescaleSlope", None), 1.0) or 1.0
    intercept = _dicom_first_number(getattr(ds, "RescaleIntercept", None), 0.0) or 0.0
    image = image * slope + intercept

    center = _dicom_first_number(getattr(ds, "WindowCenter", None))
    width = _dicom_first_number(getattr(ds, "WindowWidth", None))
    if center is not None and width and width > 0:
        low = center - width / 2
        high = center + width / 2
    else:
        low, high = np.percentile(image, [1, 99])
        if high <= low:
            high = low + 1

    image = np.clip((image - low) * 255.0 / (high - low), 0, 255).astype("uint8")
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        image = 255 - image
    return image


def _convert_dicom_to_jpeg(filepath: Path) -> tuple[Path, str, str, dict] | None:
    try:
        pydicom = importlib.import_module("pydicom")
        ds = pydicom.dcmread(str(filepath), force=True)
        pixels = ds.pixel_array
        normalized = _normalize_dicom_pixels(ds, pixels)
        image = Image.fromarray(normalized)
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        converted_path = filepath.with_suffix(".jpg")
        image.save(str(converted_path), format="JPEG", quality=92, optimize=True)
        with open(str(converted_path), "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        return converted_path, image_b64, "image/jpeg", _extract_dicom_metadata(ds)
    except Exception:
        logger.exception("Falha ao converter DICOM %s", filepath.name)
        return None


def _dicom_context(upload_items: list[dict]) -> str:
    parts = []
    for item in upload_items:
        metadata = item.get("dicom_metadata") or {}
        if not metadata:
            continue
        values = []
        if metadata.get("modality"):
            values.append(f"Modalidade DICOM: {metadata['modality']}")
        if metadata.get("body_part"):
            values.append(f"Região DICOM: {metadata['body_part']}")
        if metadata.get("study_description"):
            values.append(f"Estudo: {metadata['study_description']}")
        if metadata.get("series_description"):
            values.append(f"Série: {metadata['series_description']}")
        if values:
            parts.append("; ".join(values))
    return " | ".join(parts)


def _description_with_dicom_context(user_description: str, upload_items: list[dict]) -> str:
    dicom_context = _dicom_context(upload_items)
    if not dicom_context:
        return user_description
    if user_description:
        return f"{user_description} | {dicom_context}"
    return dicom_context


def _env_value(*names: str) -> str:
    """Retorna a primeira variável de ambiente preenchida."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def get_api_key() -> str:
    return _env_value("GEMINI_API_KEY", "GOOGLE_API_KEY")


def get_anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def get_model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _gemini_key_missing_message() -> str:
    if _is_local_runtime():
        return (
            "Erro: chave do Gemini não configurada. No ambiente local, edite o arquivo .env "
            "e preencha GEMINI_API_KEY=sua_chave. Também aceito GOOGLE_API_KEY como alias."
        )
    return (
        "Erro: GEMINI_API_KEY não configurada. Adicione a variável de ambiente no painel "
        "do Vercel (Settings -> Environment Variables)."
    )


def _gemini_key_invalid_message() -> str:
    if _is_local_runtime():
        return "Erro de autenticação: chave do Gemini inválida. Verifique GEMINI_API_KEY no arquivo .env local."
    return "Erro de autenticação: GEMINI_API_KEY inválida. Verifique a chave no painel do Vercel."


def save_upload_file(file) -> dict | None:
    """
    Salva um arquivo de upload em disco.
    Retorna metadados da imagem processada ou None se inválido.
    """
    if not file or file.filename == "" or not allowed_file(file.filename):
        return None

    original_name = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    filepath = UPLOAD_FOLDER / unique_name
    file.save(str(filepath))

    ext = filepath.suffix.lower().lstrip(".")
    if ext == "dcm":
        dicom_result = _convert_dicom_to_jpeg(filepath)
        if not dicom_result:
            try:
                filepath.unlink()
            except Exception:
                pass
            return None
        converted_path, image_b64, image_mime, dicom_metadata = dicom_result
        return {
            "filepath": converted_path,
            "display_b64": image_b64,
            "display_mime": image_mime,
            "storage_mime": "application/dicom",
            "source": "dicom",
            "original_name": original_name,
            "original_path": filepath,
            "dicom_metadata": dicom_metadata,
        }

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp", "gif": "image/gif",
    }
    image_mime = mime_map.get(ext, "image/jpeg")

    with open(str(filepath), "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    return {
        "filepath": filepath,
        "display_b64": image_b64,
        "display_mime": image_mime,
        "storage_mime": image_mime,
        "source": "upload",
        "original_name": original_name,
        "original_path": filepath,
        "dicom_metadata": {},
    }


# ── Acesso admin (testes internos) ───────────────────────────────────────────

@app.route("/admin/entrar")
def admin_entrar():
    """
    Concede acesso de teste via cookie admin.
    Protegido pela variável de ambiente ADMIN_KEY.
    Em ambiente local, se ADMIN_KEY não estiver definida, uma chave temporária é
    gerada por processo e registrada no log de inicialização (ver _GENERATED_LOCAL_ADMIN_KEY).
    Uso: /admin/entrar?key=SUA_ADMIN_KEY
    Para revogar: /admin/sair
    """
    admin_key = _get_admin_key()
    if not admin_key:
        return "ADMIN_KEY não configurada no servidor. Defina ADMIN_KEY nas variáveis de ambiente.", 403

    provided = request.args.get("key", "").strip()
    if not provided or not hmac.compare_digest(provided, admin_key):
        return "Chave inválida.", 403

    token = _serializer().dumps({"admin": True}, salt=_ADMIN_SALT)
    resp = make_response(redirect(url_for("index")))
    resp.set_cookie(
        _ADMIN_COOKIE,
        token,
        max_age=30 * 24 * 3600,   # 30 dias
        httponly=True,
        samesite="Lax",
        secure=_secure_cookie(),
    )
    return resp


@app.route("/admin/sair")
def admin_sair():
    """Remove o cookie de acesso admin."""
    resp = make_response(redirect(url_for("index")))
    resp.delete_cookie(_ADMIN_COOKIE)
    return resp


# ── Orçamento de custo diário (Gemini + Claude) ────────────────────────────────
#
# Protege contra estouro de custo (ex.: abuso do trial gratuito) limitando o
# gasto diário por escopo. Cada cap é opcional (0 = desativado); o cap do
# trial vem habilitado por padrão pois é o vetor de abuso mais óbvio (uso
# gratuito sem limite de gasto agregado).

def _budget_user_key(mode: str) -> str | None:
    """Identificador do 'usuário' para o cap de orçamento por usuário, por modo."""
    if mode == "premium":
        return g.get("user_email") or None
    if mode == "api":
        supplied_key = request.headers.get("X-API-Key", "").strip()
        if not supplied_key:
            return None
        return f"apikey:{hashlib.sha256(supplied_key.encode('utf-8')).hexdigest()[:16]}"
    return None


def _cost_budget_scopes(mode: str) -> list[tuple[str, float]]:
    """Escopos [(scope, cap_usd), ...] a verificar/atualizar para esta análise."""
    scopes = []
    global_cap = float(os.environ.get("GLOBAL_DAILY_COST_BUDGET_USD", "0"))
    if global_cap > 0:
        scopes.append(("global", global_cap))

    if mode == "trial":
        trial_cap = float(os.environ.get("TRIAL_DAILY_COST_BUDGET_USD", "3.00"))
        if trial_cap > 0:
            scopes.append(("trial", trial_cap))

    user_cap = float(os.environ.get("USER_DAILY_COST_BUDGET_USD", "0"))
    user_key = _budget_user_key(mode)
    if user_key and user_cap > 0:
        scopes.append((f"user:{user_key}", user_cap))

    return scopes


def _blocked_by_cost_budget(mode: str) -> bool:
    for scope, cap in _cost_budget_scopes(mode):
        if cost_tracking.is_budget_exceeded(scope, cap):
            logger.warning("Bloqueado por orçamento de custo diário excedido (scope=%s, cap_usd=%.2f)", scope, cap)
            return True
    return False


def _own_cost_usage(mode: str, usage: list[dict]) -> list[dict]:
    """
    Uso de tokens que sai do nosso orçamento. No modo API, se o chamador
    informou sua própria chave Gemini (X-API-Key), o custo do Gemini é dele —
    só a parcela do Claude (sempre com nossa chave, usada na síntese dual)
    continua contando para o nosso orçamento.
    """
    if mode == "api" and request.headers.get("X-API-Key", "").strip():
        return [item for item in usage if not item["model"].startswith("gemini")]
    return usage


def _record_analysis_cost(mode: str, result: dict) -> None:
    """Loga o custo total da análise (visibilidade) e credita apenas o custo que é nosso nos orçamentos."""
    usage = result.get("usage", [])
    cost_tracking.log_analysis_cost(result.get("exam_type", ""), result.get("model_used", ""), usage)

    own_usage = _own_cost_usage(mode, usage)
    own_cost_usd = sum(
        cost_tracking.estimate_cost_usd(item["model"], item["input_tokens"], item["output_tokens"])
        for item in own_usage
    )
    if own_cost_usd > 0:
        for scope, _ in _cost_budget_scopes(mode):
            cost_tracking.add_spend(scope, own_cost_usd)


# ── Fila de análise assíncrona ─────────────────────────────────────────────────
#
# Quando um Redis real está configurado (jobs.is_async_enabled()), as rotas de
# análise enfileiram o trabalho e respondem em milissegundos com um job_id,
# em vez de bloquear a requisição HTTP por até ~90s. Um worker separado
# (worker.py) consome a fila. Sem Redis configurado, as rotas caem no
# caminho síncrono existente — nada muda para quem não configurar a fila.

def _read_upload_bytes(upload_items: list[dict]) -> tuple[list[tuple[bytes, str]], list[dict]]:
    """Lê os bytes de cada upload (para enviar ao job) e os metadados de persistência."""
    images = []
    image_meta = []
    for upload_item in upload_items:
        source_path = Path(upload_item.get("original_path") or upload_item["filepath"])
        raw = source_path.read_bytes()
        images.append((raw, upload_item.get("storage_mime") or upload_item["display_mime"]))
        image_meta.append({
            "origem": upload_item.get("source") or "upload",
            "arquivo_original": upload_item.get("original_name") or "",
            "dicom_metadata": upload_item.get("dicom_metadata") or None,
        })
    return images, image_meta


def _cleanup_upload_files(upload_items: list[dict]) -> None:
    paths = set()
    for upload_item in upload_items:
        for key in ("filepath", "original_path"):
            if upload_item.get(key):
                paths.add(str(upload_item[key]))
    for fp in paths:
        try:
            Path(fp).unlink()
        except Exception:
            pass


def _enqueue_analysis_job(
    mode: str,
    images: list[tuple[bytes, str]],
    image_meta: list[dict],
    exam_filename: str,
    api_key: str,
    user_description: str,
    persist: dict,
    display_extra: dict | None = None,
) -> str | None:
    """Enfileira a análise. Retorna o job_id, ou None se a fila não está disponível."""
    exclude_gemini_cost = mode == "api" and bool(request.headers.get("X-API-Key", "").strip())
    return jobs.enqueue_analysis(
        mode=mode,
        images=images,
        image_meta=image_meta,
        exam_filename=exam_filename,
        api_key=api_key,
        user_description=user_description,
        model_name=get_model_name(),
        anthropic_api_key=get_anthropic_api_key(),
        exclude_gemini_cost=exclude_gemini_cost,
        cost_scopes=_cost_budget_scopes(mode),
        persist=persist,
        display_extra=display_extra,
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """
    Verifica conectividade com as dependências externas (Redis da fila
    assíncrona e Supabase). Usado por monitoramento externo do worker
    (Railway/Render), que roda como processo separado sem endpoint próprio.
    Cada dependência é "disabled" se não configurada, "ok"/"down" se configurada.
    """
    checks = {}
    degraded = False

    if jobs.is_async_enabled():
        redis_ok = jobs.health_check_redis()
        checks["redis"] = "ok" if redis_ok else "down"
        degraded = degraded or not redis_ok
    else:
        checks["redis"] = "disabled"

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        supabase_ok = db.health_check()
        checks["supabase"] = "ok" if supabase_ok else "down"
        degraded = degraded or not supabase_ok
    else:
        checks["supabase"] = "disabled"

    body = {
        "status": "degraded" if degraded else "ok",
        "build_version": BUILD_VERSION,
        "checks": checks,
    }
    return jsonify(body), 503 if degraded else 200


# ── Rotas principais ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", premium=is_premium(request))


@app.route("/analyze", methods=["POST"])
@premium_required
def analyze():
    """Endpoint para receber e analisar o(s) exame(s) médico(s)."""
    g.modo = "premium"
    t0 = time.time()

    if not _has_privacy_consent():
        flash("Confirme o consentimento LGPD e a ciência de que a IA é ferramenta de apoio antes de enviar o exame.", "error")
        return redirect(url_for("index"))

    responsible_info = _responsible_info_from_form()
    if not responsible_info["name"] or not responsible_info["role"]:
        flash("Informe o nome e o perfil profissional do responsável pela análise.", "error")
        return redirect(url_for("index"))

    api_key = get_api_key()
    if not api_key:
        flash(_gemini_key_missing_message(), "error")
        return redirect(url_for("index"))

    if _blocked_by_cost_budget("premium"):
        flash("Limite de gasto diário atingido. Tente novamente mais tarde ou contate o suporte.", "error")
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

    upload_items = []
    images_data = []  # Lista de {"b64": str, "mime": str, "source": str} para exibição no resultado
    for file in files:
        upload_item = save_upload_file(file)
        if upload_item is None:
            continue
        upload_items.append(upload_item)
        images_data.append({
            "b64": upload_item["display_b64"],
            "mime": upload_item["display_mime"],
            "source": upload_item["source"],
        })

    if not upload_items:
        flash(f"Formato de arquivo não suportado. Use: {', '.join(ALLOWED_EXTENSIONS)}", "error")
        return redirect(url_for("index"))

    analysis_description = _description_with_dicom_context(user_description, upload_items)
    images, image_meta = _read_upload_bytes(upload_items)
    exam_filename = upload_items[0].get("original_name") or "exame.jpg"
    _cleanup_upload_files(upload_items)  # os bytes já foram lidos; não precisamos mais do disco

    if jobs.is_async_enabled():
        job_id = _enqueue_analysis_job(
            mode="premium",
            images=images,
            image_meta=image_meta,
            exam_filename=exam_filename,
            api_key=api_key,
            user_description=analysis_description,
            persist={
                "descricao_usuario": user_description,
                "modalidade": _detect_modalidade(analysis_description),
                "ip_address": _get_ip(),
                "user_agent": request.headers.get("User-Agent"),
                "cliente_id": g.get("cliente_id"),
                "responsavel": responsible_info,
            },
            display_extra={
                "images_data": images_data,
                "user_description_display": user_description,
                "responsible": responsible_info,
            },
        )
        if job_id:
            return redirect(url_for("analyze_processando", job_id=job_id))

    # Fallback síncrono (fila indisponível): mesma análise, sem enfileirar.
    try:
        result = analyze_exam_from_bytes(
            exam_images_data=images,
            exam_filename=exam_filename,
            api_key=api_key,
            user_description=analysis_description,
            model_name=get_model_name(),
            anthropic_api_key=get_anthropic_api_key(),
        )
        _record_analysis_cost("premium", result)

        # Persiste análise e imagens no banco
        analise_id = db.salvar_analise(
            tipo_exame=result["exam_type"],
            analise_completa=result["analysis"],
            modelo_ia=result["model_used"],
            referencias_usadas=result["references_used"],
            num_imagens=result["num_images"],
            modo="premium",
            descricao_usuario=user_description,
            modalidade=_detect_modalidade(analysis_description),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            tempo_ms=int((time.time() - t0) * 1000),
            cliente_id=g.get("cliente_id"),
            responsavel=responsible_info,
        )
        if analise_id:
            g.analise_id = analise_id
            for i, (raw_bytes, mime) in enumerate(images, 1):
                meta = image_meta[i - 1]
                db.salvar_imagem_exame(
                    analise_id=analise_id,
                    mime_type=mime,
                    tamanho_bytes=len(raw_bytes),
                    hash_md5=hashlib.md5(raw_bytes).hexdigest(),
                    ordem=i,
                    origem=meta.get("origem") or "upload",
                    arquivo_original=meta.get("arquivo_original") or "",
                    dicom_metadata=meta.get("dicom_metadata") or None,
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
            responsible=responsible_info,
            analysis_id=analise_id,
            consensus=_build_consensus_info(result["model_used"], result["analysis"]),
        )

    except Exception as e:
        error_msg = str(e)
        logger.exception("Falha na análise de exame (%s)", type(e).__name__)
        error_lower = error_msg.lower()
        error_code = getattr(e, "code", None)
        if "api key not valid" in error_lower or "invalid api key" in error_lower or "api_key_invalid" in error_lower:
            flash(_gemini_key_invalid_message(), "error")
        elif error_code == 403 or "permission_denied" in error_lower:
            flash("Erro de permissão na API de IA — verifique o billing/configuração do projeto Google Cloud. A equipe já foi notificada.", "error")
        elif "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
            flash("Limite de requisições atingido. Aguarde alguns instantes e tente novamente.", "error")
        elif error_code == 503 or "unavailable" in error_lower or "overloaded" in error_lower:
            flash("O serviço de IA está temporariamente sobrecarregado. Aguarde alguns segundos e tente novamente.", "error")
        elif error_code == 404 or "not found" in error_lower:
            flash("Modelo de IA temporariamente indisponível. Tente novamente em instantes.", "error")
        else:
            flash("Não foi possível processar o exame. Tente novamente.", "error")
        return redirect(url_for("index"))


@app.route("/analyze/processando/<job_id>")
@premium_required
def analyze_processando(job_id: str):
    """Página de espera (poll) enquanto a análise premium é processada de forma assíncrona."""
    return render_template(
        "processando.html",
        status_url=url_for("analyze_status", job_id=job_id),
        resultado_url=url_for("analyze_resultado", job_id=job_id),
    )


@app.route("/analyze/status/<job_id>")
@premium_required
@rate_limit("STATUS_POLL_RATE_LIMIT", "60 per minute")
def analyze_status(job_id: str):
    """Consulta o status de uma análise premium enfileirada (usado pela página de espera)."""
    status = jobs.get_job_status(job_id)
    return jsonify({"status": status["status"], "error": status.get("error", "")})


@app.route("/analyze/resultado/<job_id>")
@premium_required
def analyze_resultado(job_id: str):
    """Renderiza o resultado de uma análise premium assíncrona já concluída."""
    status = jobs.get_job_status(job_id)
    if status["status"] != "finished":
        return redirect(url_for("analyze_processando", job_id=job_id))

    result = status["result"]
    return render_template(
        "result.html",
        analysis=result["analysis"],
        exam_type=result["exam_type"].replace("_", " ").title(),
        references_used=result["references_used"],
        model_used=result["model_used"],
        images=result.get("images_data") or [],
        num_images=result["num_images"],
        user_description=result.get("user_description_display", ""),
        responsible=result.get("responsible") or {"name": "", "role": ""},
        analysis_id=result.get("analise_id"),
        consensus=_build_consensus_info(result["model_used"], result["analysis"]),
    )


@app.route("/trial")
def trial():
    """Página de teste gratuito, embutida via iframe na landing page (WordPress)."""
    return render_template("trial.html")


@app.route("/trial/analyze", methods=["POST"])
@rate_limit("TRIAL_RATE_LIMIT", "10 per hour")
def trial_analyze():
    """Endpoint para análise no modo de teste gratuito (retorna JSON, aceita apenas 1 imagem)."""
    g.modo = "trial"
    t0 = time.time()

    if not _has_privacy_consent():
        return jsonify({"error": "Confirme o consentimento LGPD e a ciência de que a IA é ferramenta de apoio."}), 400

    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "Serviço temporariamente indisponível. Tente novamente mais tarde."}), 503

    if _blocked_by_cost_budget("trial"):
        return jsonify({"error": "Limite de uso gratuito diário atingido. Tente novamente mais tarde."}), 429

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

    image_b64 = result_save["display_b64"]
    image_mime = result_save["display_mime"]
    analysis_description = _description_with_dicom_context(user_description, [result_save])
    images, image_meta = _read_upload_bytes([result_save])
    exam_filename = result_save.get("original_name") or "exame.jpg"
    _cleanup_upload_files([result_save])

    if jobs.is_async_enabled():
        job_id = _enqueue_analysis_job(
            mode="trial",
            images=images,
            image_meta=image_meta,
            exam_filename=exam_filename,
            api_key=api_key,
            user_description=analysis_description,
            persist={
                "descricao_usuario": user_description,
                "modalidade": _detect_modalidade(analysis_description),
                "ip_address": _get_ip(),
                "user_agent": request.headers.get("User-Agent"),
            },
            display_extra={"image_b64": image_b64, "image_mime": image_mime},
        )
        if job_id:
            return jsonify({
                "status": "queued",
                "job_id": job_id,
                "status_url": url_for("trial_analyze_status", job_id=job_id),
            }), 202

    # Fallback síncrono (fila indisponível): mesma análise, sem enfileirar.
    try:
        result = analyze_exam_from_bytes(
            exam_images_data=images,
            exam_filename=exam_filename,
            api_key=api_key,
            user_description=analysis_description,
            model_name=get_model_name(),
            anthropic_api_key=get_anthropic_api_key(),
        )
        _record_analysis_cost("trial", result)

        analise_id = db.salvar_analise(
            tipo_exame=result["exam_type"],
            analise_completa=result["analysis"],
            modelo_ia=result["model_used"],
            referencias_usadas=result["references_used"],
            num_imagens=1,
            modo="trial",
            descricao_usuario=user_description,
            modalidade=_detect_modalidade(analysis_description),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            tempo_ms=int((time.time() - t0) * 1000),
        )
        if analise_id:
            g.analise_id = analise_id
            raw_bytes, mime = images[0]
            db.salvar_imagem_exame(
                analise_id=analise_id,
                mime_type=mime,
                tamanho_bytes=len(raw_bytes),
                hash_md5=hashlib.md5(raw_bytes).hexdigest(),
                origem=image_meta[0].get("origem") or "upload",
                arquivo_original=image_meta[0].get("arquivo_original") or "",
                dicom_metadata=image_meta[0].get("dicom_metadata") or None,
            )

        return jsonify({
            "analysis": result["analysis"],
            "exam_type": result["exam_type"].replace("_", " ").title(),
            "references_used": result["references_used"],
            "model_used": result["model_used"],
            "consensus": _build_consensus_info(result["model_used"], result["analysis"]),
            "image_b64": image_b64,
            "image_mime": image_mime,
        }), 200

    except Exception as e:
        error_msg = str(e)
        logger.exception("Falha na análise trial (%s)", type(e).__name__)
        error_lower = error_msg.lower()
        error_code = getattr(e, "code", None)
        if "api key not valid" in error_lower or "invalid api key" in error_lower:
            return jsonify({"error": "Serviço temporariamente indisponível."}), 503
        elif error_code == 403 or "permission_denied" in error_lower:
            return jsonify({"error": "Erro de permissão na API de IA — verifique o billing/configuração do projeto Google Cloud."}), 503
        elif "quota" in error_lower or "rate limit" in error_lower or "resource_exhausted" in error_lower:
            return jsonify({"error": "Limite de requisições atingido. Tente novamente em alguns instantes."}), 429
        elif error_code == 503 or "unavailable" in error_lower or "overloaded" in error_lower:
            return jsonify({"error": "Serviço de IA sobrecarregado. Aguarde alguns segundos e tente novamente."}), 503
        elif error_code == 404 or "not found" in error_lower:
            return jsonify({"error": "Serviço de IA temporariamente indisponível. Tente novamente."}), 503
        return jsonify({"error": "Não foi possível processar o exame. Tente novamente."}), 500


@app.route("/trial/analyze/status/<job_id>")
@rate_limit("STATUS_POLL_RATE_LIMIT", "60 per minute")
def trial_analyze_status(job_id: str):
    """Consulta o status de uma análise trial enfileirada."""
    status = jobs.get_job_status(job_id)
    if status["status"] == "finished":
        result = status["result"]
        return jsonify({
            "status": "finished",
            "analysis": result["analysis"],
            "exam_type": result["exam_type"].replace("_", " ").title(),
            "references_used": result["references_used"],
            "model_used": result["model_used"],
            "consensus": _build_consensus_info(result["model_used"], result["analysis"]),
            "image_b64": result.get("image_b64", ""),
            "image_mime": result.get("image_mime", ""),
        }), 200
    if status["status"] == "not_found":
        return jsonify({"status": "not_found", "error": "Job não encontrado."}), 404
    return jsonify(status), 200


@app.route("/api/analyze", methods=["POST"])
@rate_limit("API_RATE_LIMIT", "60 per hour")
def api_analyze():
    """Endpoint REST para integração programática. Suporta múltiplas imagens."""
    g.modo = "api"
    t0 = time.time()

    api_key = request.headers.get("X-API-Key") or get_api_key()
    if not api_key:
        return jsonify({"error": "API key não fornecida"}), 401

    if _blocked_by_cost_budget("api"):
        return jsonify({"error": "Limite de gasto diário atingido para esta chave. Tente novamente mais tarde."}), 429

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

    upload_items = []
    for file in files:
        upload_item = save_upload_file(file)
        if upload_item is None:
            continue
        upload_items.append(upload_item)

    if not upload_items:
        return jsonify({"error": "Nenhum arquivo válido pôde ser processado"}), 400

    analysis_description = _description_with_dicom_context(user_description, upload_items)
    images, image_meta = _read_upload_bytes(upload_items)
    exam_filename = upload_items[0].get("original_name") or "exame.jpg"
    _cleanup_upload_files(upload_items)  # os bytes já foram lidos; não precisamos mais do disco

    if jobs.is_async_enabled():
        job_id = _enqueue_analysis_job(
            mode="api",
            images=images,
            image_meta=image_meta,
            exam_filename=exam_filename,
            api_key=api_key,
            user_description=analysis_description,
            persist={
                "descricao_usuario": user_description,
                "modalidade": _detect_modalidade(analysis_description),
                "ip_address": _get_ip(),
                "user_agent": request.headers.get("User-Agent"),
            },
        )
        if job_id:
            return jsonify({
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "status_url": url_for("api_analyze_status", job_id=job_id),
            }), 202

    # Fallback síncrono (fila indisponível): mesma análise, sem enfileirar.
    try:
        result = analyze_exam_from_bytes(
            exam_images_data=images,
            exam_filename=exam_filename,
            api_key=api_key,
            user_description=analysis_description,
            model_name=get_model_name(),
            anthropic_api_key=get_anthropic_api_key(),
        )
        _record_analysis_cost("api", result)

        analise_id = db.salvar_analise(
            tipo_exame=result["exam_type"],
            analise_completa=result["analysis"],
            modelo_ia=result["model_used"],
            referencias_usadas=result["references_used"],
            num_imagens=result["num_images"],
            modo="api",
            descricao_usuario=user_description,
            modalidade=_detect_modalidade(analysis_description),
            ip_address=_get_ip(),
            user_agent=request.headers.get("User-Agent"),
            tempo_ms=int((time.time() - t0) * 1000),
        )
        if analise_id:
            g.analise_id = analise_id
            for i, (raw_bytes, mime) in enumerate(images, 1):
                meta = image_meta[i - 1]
                db.salvar_imagem_exame(
                    analise_id=analise_id,
                    mime_type=mime,
                    tamanho_bytes=len(raw_bytes),
                    hash_md5=hashlib.md5(raw_bytes).hexdigest(),
                    ordem=i,
                    origem=meta.get("origem") or "upload",
                    arquivo_original=meta.get("arquivo_original") or "",
                    dicom_metadata=meta.get("dicom_metadata") or None,
                )

        result["consensus"] = _build_consensus_info(result.get("model_used", ""), result.get("analysis", ""))
        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


@app.route("/api/analyze/status/<job_id>")
@rate_limit("STATUS_POLL_RATE_LIMIT", "60 per minute")
def api_analyze_status(job_id: str):
    """Consulta o status de uma análise enfileirada via /api/analyze."""
    status = jobs.get_job_status(job_id)
    if status["status"] == "finished":
        result = dict(status["result"])
        result["consensus"] = _build_consensus_info(result.get("model_used", ""), result.get("analysis", ""))
        return jsonify({"status": "finished", **result}), 200
    if status["status"] == "not_found":
        return jsonify({"status": "not_found", "error": "Job não encontrado."}), 404
    return jsonify(status), 200


# ── Histórico e feedback médico ───────────────────────────────────────────────

@app.route("/laudos")
@premium_required
def laudos():
    """Lista laudos salvos do usuário premium atual."""
    g.modo = "premium"
    include_all = bool(g.get("is_admin"))
    rows = db.listar_analises(
        cliente_id=g.get("cliente_id"),
        limit=80,
        include_all=include_all,
    )
    return render_template(
        "laudos.html",
        analyses=rows,
        is_admin=include_all,
    )


@app.route("/laudos/<analise_id>")
@premium_required
def laudo_detalhe(analise_id: str):
    """Abre um laudo salvo no histórico."""
    g.modo = "premium"
    include_all = bool(g.get("is_admin"))
    row = db.buscar_analise(
        analise_id=analise_id,
        cliente_id=g.get("cliente_id"),
        include_all=include_all,
    )
    if not row:
        flash("Laudo não encontrado no histórico deste acesso.", "error")
        return redirect(url_for("laudos"))

    responsible = _responsible_info_from_row(row)
    return render_template(
        "laudo_detalhe.html",
        laudo=row,
        analysis=row.get("analise_completa") or "",
        analysis_id=row.get("id"),
        exam_type=_format_exam_label(row.get("tipo_exame")),
        user_description=row.get("descricao_usuario") or "",
        responsible=responsible,
        consensus=_build_consensus_info(row.get("modelo_ia") or "", row.get("analise_completa") or ""),
    )


@app.route("/clinica/dashboard")
@premium_required
def dashboard_clinica():
    """Dashboard operacional da clínica/usuário premium."""
    g.modo = "premium"
    include_all = bool(g.get("is_admin"))
    analyses = db.listar_analises(
        cliente_id=g.get("cliente_id"),
        limit=500,
        include_all=include_all,
    )
    analysis_ids = [item.get("id") for item in analyses if item.get("id")]
    validations = db.listar_validacoes_clinicas(analysis_ids)
    feedbacks = db.listar_feedbacks_por_analises(analysis_ids)
    metrics = db.montar_metricas_dashboard(analyses, validations, feedbacks)

    validation_by_analysis = {}
    for validation in validations:
        analysis_id = validation.get("analise_id")
        if analysis_id and analysis_id not in validation_by_analysis:
            validation_by_analysis[analysis_id] = validation

    pending = [item for item in analyses if item.get("id") not in validation_by_analysis]

    return render_template(
        "dashboard_clinica.html",
        metrics=metrics,
        analyses=analyses[:12],
        pending=pending[:12],
        validations=validations[:12],
        is_admin=include_all,
    )


@app.route("/feedback", methods=["POST"])
@premium_required
def feedback():
    """Recebe validação/correção médica sobre um laudo gerado."""
    g.modo = "premium"
    data = request.get_json(silent=True) or request.form
    analise_id = str(data.get("analysis_id") or "").strip()
    feedback_value = str(data.get("feedback") or "").strip()
    comentario = str(data.get("comment") or "").strip()[:3000]
    responsible_name = str(data.get("responsible_name") or "").strip()[:160]
    responsible_role = str(data.get("responsible_role") or "").strip()[:80]

    feedback_map = {
        "agree": {
            "tipo": "validacao",
            "label": "Concordo totalmente",
            "concordancia": True,
            "grau": 100,
        },
        "partial": {
            "tipo": "correcao",
            "label": "Concordo em parte",
            "concordancia": False,
            "grau": 60,
        },
        "disagree": {
            "tipo": "correcao",
            "label": "Discordo",
            "concordancia": False,
            "grau": 0,
        },
    }

    if feedback_value not in feedback_map:
        return jsonify({"error": "Tipo de feedback inválido."}), 400
    if not analise_id:
        return jsonify({"error": "Laudo sem identificação para salvar feedback."}), 400
    if feedback_value in {"partial", "disagree"} and not comentario:
        return jsonify({"error": "Descreva o que deveria ser corrigido no laudo."}), 400

    include_all = bool(g.get("is_admin"))
    row = db.buscar_analise(
        analise_id=analise_id,
        cliente_id=g.get("cliente_id"),
        include_all=include_all,
    )
    if not row:
        return jsonify({"error": "Laudo não encontrado para este acesso."}), 404

    meta = feedback_map[feedback_value]
    fonte_nome = " — ".join(part for part in [responsible_name, responsible_role] if part)
    if not fonte_nome:
        fonte_nome = g.get("user_email") or ("admin" if include_all else "")

    feedback_id = db.salvar_feedback(
        analise_id=analise_id,
        tipo=meta["tipo"],
        comentario=comentario or meta["label"],
        achado_original=row.get("analise_completa") or "",
        achado_corrigido=comentario if feedback_value in {"partial", "disagree"} else "",
        secao="validacao_medica",
        fonte=_feedback_source_for_role(responsible_role),
        fonte_nome=fonte_nome,
    )

    if not feedback_id:
        return jsonify({"error": "Não foi possível salvar o feedback agora."}), 503

    db.salvar_diagnostico_validado(
        analise_id=analise_id,
        tipo_exame=row.get("tipo_exame") or "",
        diagnostico_ia=row.get("analise_completa") or "",
        diagnostico_final=comentario or meta["label"],
        concordancia=meta["concordancia"],
        grau_concordancia=meta["grau"],
        validado_por=fonte_nome,
        modalidade=row.get("modalidade"),
    )

    return jsonify({
        "success": True,
        "message": "Feedback médico salvo no histórico de validação.",
    })


@app.route("/validacao-clinica", methods=["POST"])
@premium_required
def validacao_clinica():
    """Registra validação clínica formal de um laudo salvo."""
    g.modo = "premium"
    data = request.get_json(silent=True) or request.form
    wants_json = request.is_json or "application/json" in (request.headers.get("Accept") or "")

    analise_id = str(data.get("analysis_id") or "").strip()
    final_diagnosis = str(data.get("final_diagnosis") or "").strip()[:6000]
    reviewer_name = str(data.get("reviewer_name") or "").strip()[:160]
    reviewer_register = str(data.get("reviewer_register") or "").strip()[:100]
    reviewer_role = str(data.get("reviewer_role") or "").strip()[:80]
    score = _score_value(data.get("concordance_score"), 100)
    correct_findings = _split_validation_lines(str(data.get("correct_findings") or ""))
    missed_findings = _split_validation_lines(str(data.get("missed_findings") or ""))
    wrong_findings = _split_validation_lines(str(data.get("wrong_findings") or ""))
    notes = str(data.get("validation_notes") or "").strip()[:3000]

    def _validation_response(message: str, status: int = 200, ok: bool = True):
        if wants_json:
            payload = {"success": ok, "message": message} if ok else {"error": message}
            return jsonify(payload), status
        flash(message, "success" if ok else "error")
        return redirect(request.referrer or url_for("laudos"))

    if not analise_id:
        return _validation_response("Laudo sem identificação para validação.", 400, False)
    if not final_diagnosis:
        return _validation_response("Informe o diagnóstico final ou parecer validado.", 400, False)

    include_all = bool(g.get("is_admin"))
    row = db.buscar_analise(
        analise_id=analise_id,
        cliente_id=g.get("cliente_id"),
        include_all=include_all,
    )
    if not row:
        return _validation_response("Laudo não encontrado para este acesso.", 404, False)

    validado_por = " — ".join(part for part in [reviewer_name, reviewer_register or reviewer_role] if part)
    if not validado_por:
        validado_por = g.get("user_email") or ("admin" if include_all else "Profissional responsável")

    validation_id = db.salvar_diagnostico_validado(
        analise_id=analise_id,
        tipo_exame=row.get("tipo_exame") or "",
        diagnostico_ia=row.get("analise_completa") or "",
        diagnostico_final=final_diagnosis,
        concordancia=score >= 80 and not missed_findings and not wrong_findings,
        grau_concordancia=score,
        achados_corretos=correct_findings,
        achados_perdidos=missed_findings,
        achados_incorretos=wrong_findings,
        validado_por=validado_por,
        modalidade=row.get("modalidade"),
    )

    if not validation_id:
        return _validation_response("Não foi possível salvar a validação clínica agora.", 503, False)

    db.salvar_feedback(
        analise_id=analise_id,
        tipo="validacao",
        comentario=notes or f"Validação clínica formal registrada com {score}% de concordância.",
        achado_original=row.get("analise_completa") or "",
        achado_corrigido=final_diagnosis,
        secao="validacao_clinica_formal",
        fonte=_feedback_source_for_role(reviewer_role),
        fonte_nome=validado_por,
    )

    return _validation_response("Validação clínica formal salva com sucesso.")


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
        logger.exception("Erro ao criar cobrança Asaas (%s)", type(e).__name__)
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
        except Exception:
            logger.exception("Erro ao obter QR PIX (payment_id=%s)", payment_id)
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
        except Exception:
            logger.exception("Erro ao obter identificação de boleto (payment_id=%s)", payment_id)
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
                httponly=True, samesite="Lax", secure=_secure_cookie(),
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
    except Exception:
        logger.exception("Erro ao verificar pagamento Asaas (payment_id=%s)", payment_id)
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
            secure=_secure_cookie(),
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

    logger.info("Webhook Asaas recebido", extra={"event": event, "payment_id": payment_id, "status": status})

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
        secure=_secure_cookie(),
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
        secure=_secure_cookie(),
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
            secure=_secure_cookie(),
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
