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

Rotação: a chave nova entra em PRONTUARIO_ENCRYPTION_KEY e as anteriores em
PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS (separadas por vírgula). Tudo continua
legível (MultiFernet tenta a atual e depois as antigas), o cadastro novo já sai
com a chave nova e `python -m scripts.prontuario_chaves rotacionar --aplicar`
recifra as linhas antigas. Só depois disso a chave antiga pode sair do ambiente.
Com PRONTUARIO_KEY_FINGERPRINT definida, uma chave diferente da esperada
desliga o prontuário em vez de misturar cifras. O mesmo script verifica (`verificar`) se a chave configurada abre todos os
prontuários — rode antes e depois de qualquer troca de variável e guarde a
impressão digital (`impressao`) junto do backup da chave.

Nome e CPF nunca vão para os provedores de IA nem para logs/PostHog.
"""

import base64
import hashlib
import hmac
import logging
import os
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

_ENV_KEY = "PRONTUARIO_ENCRYPTION_KEY"
_ENV_OLD_KEYS = "PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS"


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


def _old_keys() -> list[bytes]:
    raw = os.environ.get(_ENV_OLD_KEYS, "")
    return [k.strip().encode("ascii") for k in raw.split(",") if k.strip()]


def _primary_fernet() -> Fernet | None:
    key = _key_bytes()
    if key is None:
        return None
    try:
        return Fernet(key)
    except ValueError:
        logger.error("%s inválida: precisa ser uma chave Fernet (base64 urlsafe de 32 bytes).", _ENV_KEY)
        return None


def _old_fernets() -> list[Fernet]:
    fernets = []
    for key in _old_keys():
        try:
            fernets.append(Fernet(key))
        except ValueError:
            logger.error("Chave inválida em %s: ignorada.", _ENV_OLD_KEYS)
    return fernets


def _fernet() -> MultiFernet | None:
    """Cifra com a chave atual; decifra com a atual ou qualquer antiga."""
    primary = _primary_fernet()
    if primary is None:
        return None
    return MultiFernet([primary, *_old_fernets()])


def fingerprint(key: bytes | str | None = None) -> str:
    """Impressão digital curta da chave (não revela a chave): confere o backup sem expô-lo."""
    if key is None:
        key = _key_bytes()
    if not key:
        return ""
    if isinstance(key, str):
        key = key.encode("ascii")
    return hashlib.sha256(b"prontuario-fingerprint:" + key.strip()).hexdigest()[:16]


def fingerprint_mismatch() -> bool:
    """True se PRONTUARIO_KEY_FINGERPRINT foi definida e não bate com a chave atual.

    Protege contra colar a chave errada no ambiente: sem esta trava, o app
    gravaria pacientes novos com uma chave e não abriria os antigos — dois
    prontuários misturados, metade ilegível.
    """
    expected = os.environ.get("PRONTUARIO_KEY_FINGERPRINT", "").strip().lower()
    return bool(expected) and expected != fingerprint()


def is_configured() -> bool:
    """True se há chave para cifrar os dados do paciente (e ela é a esperada)."""
    if _fernet() is None:
        return False
    if fingerprint_mismatch():
        logger.error("PRONTUARIO_ENCRYPTION_KEY não confere com PRONTUARIO_KEY_FINGERPRINT: prontuário bloqueado.")
        return False
    return True


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


def _cpf_hash_with(key: bytes, value: str) -> str:
    return hmac.new(key, f"cpf:{cpf_digits(value)}".encode("utf-8"), hashlib.sha256).hexdigest()


def cpf_hash(value: str) -> str:
    """HMAC do CPF (só dígitos) com a chave atual — busca e unicidade sem o número em claro."""
    key = _key_bytes()
    if key is None:
        raise RuntimeError(f"{_ENV_KEY} não configurada.")
    return _cpf_hash_with(key, value)


def cpf_hash_candidates(value: str) -> list[str]:
    """HMACs do CPF com a chave atual e as antigas: acha duplicado durante uma rotação."""
    key = _key_bytes()
    if key is None:
        raise RuntimeError(f"{_ENV_KEY} não configurada.")
    hashes = [_cpf_hash_with(key, value)]
    for old in _old_keys():
        h = _cpf_hash_with(old, value)
        if h not in hashes:
            hashes.append(h)
    return hashes


def opens_with_primary(token: str | None) -> bool:
    """True se o token abre com a chave atual (não precisa ser recifrado)."""
    primary = _primary_fernet()
    if not token or primary is None:
        return False
    try:
        primary.decrypt(token.encode("ascii"))
        return True
    except (InvalidToken, ValueError):
        return False


def rotate(token: str) -> str:
    """Recifra o token com a chave atual (aceita token de chave antiga). Erro se nenhuma abre."""
    fernet = _fernet()
    if fernet is None:
        raise RuntimeError(f"{_ENV_KEY} não configurada.")
    return fernet.rotate(token.encode("ascii")).decode("ascii")
