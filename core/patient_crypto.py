"""
Dados identificáveis do paciente no prontuário (LGPD art. 11 — dado sensível).

Nome e CPF ficam cifrados no banco (Fernet: AES-128-CBC + HMAC-SHA256). Quem
ler a tabela `pacientes` direto no Supabase — backup, painel, chave vazada —
vê só texto cifrado. Para impedir CPF duplicado por profissional sem guardar o
número em claro, gravamos também um HMAC do CPF (`cpf_hash`).

A chave vem de PRONTUARIO_ENCRYPTION_KEY (gerada com
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Ela é separada do FLASK_SECRET_KEY de propósito: trocar a chave de sessão não
pode tornar ilegíveis os prontuários já gravados. Só em ambiente local/testes,
sem a variável, derivamos uma chave do FLASK_SECRET_KEY para o app funcionar.

Nome e CPF nunca vão para os provedores de IA nem para logs/PostHog.
"""

import base64
import hashlib
import hmac
import logging
import os
import re

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENV_KEY = "PRONTUARIO_ENCRYPTION_KEY"


def _is_production_like() -> bool:
    values = (os.environ.get(name, "") for name in ("VERCEL_ENV", "FLASK_ENV", "ENV", "APP_ENV"))
    return bool(os.environ.get("VERCEL")) or any(v.lower() in {"production", "prod"} for v in values)


def _key_bytes() -> bytes | None:
    """Chave Fernet (base64 urlsafe de 32 bytes) ou None se o prontuário está desligado."""
    configured = os.environ.get(_ENV_KEY, "").strip()
    if configured:
        return configured.encode("ascii")
    if _is_production_like():
        return None
    secret = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")
    digest = hashlib.sha256(f"prontuario-local:{secret}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet | None:
    key = _key_bytes()
    if key is None:
        return None
    try:
        return Fernet(key)
    except ValueError:
        logger.error("%s inválida: precisa ser uma chave Fernet (base64 urlsafe de 32 bytes).", _ENV_KEY)
        return None


def is_configured() -> bool:
    """True se há chave para cifrar os dados do paciente."""
    return _fernet() is not None


def encrypt(value: str) -> str:
    fernet = _fernet()
    if fernet is None:
        raise RuntimeError(f"{_ENV_KEY} não configurada.")
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    """Texto em claro, ou "" se o valor está vazio ou não abre com a chave atual."""
    fernet = _fernet()
    if not token or fernet is None:
        return ""
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.error("Dado de paciente não pôde ser decifrado com a chave atual.")
        return ""


def cpf_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def is_valid_cpf(value: str | None) -> bool:
    """Valida os dígitos verificadores do CPF."""
    digits = cpf_digits(value)
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d) * weight for d, weight in zip(digits[:size], range(size + 1, 1, -1)))
        check = (total * 10) % 11 % 10
        if check != int(digits[size]):
            return False
    return True


def format_cpf(value: str | None) -> str:
    digits = cpf_digits(value)
    if len(digits) != 11:
        return value or ""
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def mask_cpf(value: str | None) -> str:
    """CPF parcialmente oculto para listagens: ***.456.789-**."""
    digits = cpf_digits(value)
    if len(digits) != 11:
        return ""
    return f"***.{digits[3:6]}.{digits[6:9]}-**"


def cpf_hash(value: str) -> str:
    """HMAC do CPF (só dígitos) — busca e unicidade sem o número em claro."""
    key = _key_bytes()
    if key is None:
        raise RuntimeError(f"{_ENV_KEY} não configurada.")
    return hmac.new(key, f"cpf:{cpf_digits(value)}".encode("utf-8"), hashlib.sha256).hexdigest()
