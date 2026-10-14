"""Ambiente de execução, planos e configuração dos modelos de IA."""

import os
import secrets
from zoneinfo import ZoneInfo

DEFAULT_FLASK_SECRET_KEY = "dev-secret-key-change-in-prod"

# Gerada uma vez por processo (nunca hardcoded/commitada) — usada apenas quando
# ADMIN_KEY não está definida e o runtime é local (ver is_local_runtime()).
GENERATED_LOCAL_ADMIN_KEY = secrets.token_urlsafe(18)

try:
    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - fallback para runtimes sem base IANA local
    LOCAL_TZ = None


def is_production_like() -> bool:
    env_values = {
        os.environ.get("VERCEL_ENV", ""),
        os.environ.get("FLASK_ENV", ""),
        os.environ.get("ENV", ""),
        os.environ.get("APP_ENV", ""),
    }
    return any(value.lower() in {"production", "prod"} for value in env_values)


def is_local_runtime() -> bool:
    return not os.environ.get("VERCEL") and not is_production_like()


def get_admin_key() -> str:
    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    if admin_key:
        return admin_key
    if is_local_runtime():
        return GENERATED_LOCAL_ADMIN_KEY
    return ""


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


# ── Modelos de IA ─────────────────────────────────────────────────────────────

def env_value(*names: str) -> str:
    """Retorna a primeira variável de ambiente preenchida."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def get_api_key() -> str:
    return env_value("GEMINI_API_KEY", "GOOGLE_API_KEY")


def get_anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def get_model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
