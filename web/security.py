"""
Cookies e tokens assinados, sessão premium e identificação do cliente.

Os tokens são assinados com o FLASK_SECRET_KEY (itsdangerous), cada tipo com o
próprio salt. A sessão premium é stateless, mas revogável: o cookie carrega a
versão de sessão do cliente (clientes.sessao_versao), conferida em
load_premium_session() a cada requisição.
"""

import hashlib
import hmac
import logging
import os
import time
from functools import wraps

from flask import Response, current_app, flash, g, redirect, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup

from core import db
from core.ttl_cache import TTLCache
from web import config

logger = logging.getLogger(__name__)

PREMIUM_COOKIE = "threehealth_premium"
PREMIUM_SALT = "premium-access-v1"
ADMIN_COOKIE = "threehealth_admin"
ADMIN_SALT = "admin-access-v1"
ADMIN_COOKIE_MAX_AGE = 30 * 24 * 3600
SETUP_SALT = "password-setup-v1"
SETUP_TOKEN_MAX_AGE = 24 * 3600
CHECKOUT_COOKIE = "threehealth_checkout"
CHECKOUT_SALT = "checkout-owner-v1"
CHECKOUT_COOKIE_MAX_AGE = 7 * 24 * 3600  # cobre o vencimento do boleto (3 dias) com folga

_LOOPBACK_ADDRS = {"127.0.0.1", "::1"}


_secret_key: str | None = None


def init_app(app) -> None:
    """Registra a chave de assinatura (chamado em create_app).

    Guardada aqui, e não lida de current_app, para que tokens possam ser
    gerados/validados fora de uma requisição (scripts, worker, testes).
    """
    global _secret_key
    _secret_key = app.secret_key


def _signing_key() -> str:
    if not _secret_key:
        raise RuntimeError("web.security.init_app(app) não foi chamado.")
    return _secret_key


def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_key())


def hash_sensitive_value(value: str | None) -> str:
    """Hash com o segredo do app — para guardar identificadores sem o valor em claro."""
    if not value:
        return ""
    seed = f"{_signing_key()}:{value}".encode("utf-8", "ignore")
    return hashlib.sha256(seed).hexdigest()[:24]


# ── IP do cliente e cookies ───────────────────────────────────────────────────

def get_ip() -> str | None:
    """Retorna o IP real do cliente, usado em rate limit, orçamento e logs.

    O cliente controla o X-Forwarded-For que envia, então o primeiro item da
    lista não é confiável: trocá-lo a cada requisição burlava o rate limit do
    trial e da API. Só valem cabeçalhos escritos por proxies que controlamos:
    - Vercel: X-Real-IP / X-Vercel-Forwarded-For, definidos pela plataforma.
    - Outros (Railway, Render, nginx): TRUSTED_PROXY_HOPS=N usa o N-ésimo item
      a partir da DIREITA do X-Forwarded-For (o que o nosso proxy anexou).
    - Sem proxy: o endereço da conexão.
    """
    if os.environ.get("VERCEL"):
        for header in ("X-Real-IP", "X-Vercel-Forwarded-For"):
            value = request.headers.get(header, "").split(",")[0].strip()
            if value:
                return value

    try:
        hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))
    except ValueError:
        hops = 0
    if hops > 0:
        chain = [part.strip() for part in request.headers.get("X-Forwarded-For", "").split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]

    return request.remote_addr or None


def rate_limit_key() -> str:
    return get_ip() or "anonymous"


def secure_cookie() -> bool:
    host = (request.host or "").split(":", 1)[0]
    if host in {"127.0.0.1", "localhost", "::1"}:
        return False
    return request.is_secure or config.is_production_like() or bool(os.environ.get("VERCEL"))


def set_signed_cookie(resp, name: str, value: str, max_age: int):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="Lax", secure=secure_cookie())
    return resp


# ── Token de definição de senha (24h, uso único) ──────────────────────────────

def password_fingerprint(email: str) -> str:
    """Impressão da senha atual: o token deixa de valer assim que a senha muda (uso único)."""
    senha_hash = db.buscar_senha_hash_cliente(email) or ""
    return hashlib.sha256(senha_hash.encode("utf-8")).hexdigest()[:16]


def generate_setup_token(email: str) -> str:
    return serializer().dumps({"email": email, "h": password_fingerprint(email)}, salt=SETUP_SALT)


def verify_setup_token(token: str) -> str | None:
    """Retorna o e-mail contido no token ou None se inválido/expirado/já usado."""
    try:
        data = serializer().loads(token, salt=SETUP_SALT, max_age=SETUP_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    email = data.get("email")
    if not email or not hmac.compare_digest(str(data.get("h", "")), password_fingerprint(email)):
        return None
    return email


# ── Token premium ─────────────────────────────────────────────────────────────

def generate_premium_token(
    payment_id: str,
    email: str,
    cookie_max_age: int = 365 * 24 * 3600,
    session_version: int = 0,
) -> str:
    expires_at = int(time.time()) + cookie_max_age
    return serializer().dumps(
        {"pid": payment_id, "email": email, "exp": expires_at, "sv": session_version}, salt=PREMIUM_SALT
    )


def verify_premium_token(token: str) -> dict | None:
    try:
        # max_age generoso; a expiração real é o campo "exp" embutido.
        data = serializer().loads(token, salt=PREMIUM_SALT, max_age=10 * 365 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return None
    if "exp" in data:
        if data["exp"] < time.time():
            return None
    else:
        # Tokens legados (sem "exp"): limite original de 1 ano.
        try:
            serializer().loads(token, salt=PREMIUM_SALT, max_age=365 * 24 * 3600)
        except (BadSignature, SignatureExpired):
            return None
    return data


def premium_payload_from_request(req) -> dict | None:
    token = req.cookies.get(PREMIUM_COOKIE)
    if not token:
        return None
    return verify_premium_token(token)


# Versão de sessão por e-mail, para não consultar o banco a cada requisição
# (páginas, polling de status…). Uma revogação feita por este processo é vista
# na hora (forget_session); feita por outra instância, em até o TTL.
_session_cache = TTLCache(
    maxsize=4096, ttl_seconds=float(os.environ.get("SESSION_CACHE_TTL_SECONDS", "60"))
)
_NO_SESSION = object()


def _cached_session(email: str) -> dict | None:
    cached = _session_cache.get(email, _NO_SESSION)
    if cached is not _NO_SESSION:
        return cached
    sessao = db.buscar_sessao_cliente(email)
    if sessao is not None:
        _session_cache.set(email, sessao)
    return sessao


def forget_session(email: str) -> None:
    """Descarta a versão de sessão em cache (chamar após revogar)."""
    _session_cache.pop(email)


def revoke_sessions(email: str) -> None:
    db.revogar_sessoes_cliente(email)
    forget_session(email)


def load_premium_session() -> None:
    """Preenche g.premium_payload / g.user_email / g.cliente_id a partir do cookie."""
    payload = premium_payload_from_request(request)
    if not (payload and payload.get("email")):
        g.premium_payload = payload
        return

    email = str(payload.get("email", "")).strip().lower()
    sessao = _cached_session(email)
    # Cadastro inexistente com banco no ar = conta excluída pelo titular: o cookie
    # de outro aparelho não pode recriá-la (antes havia um upsert aqui).
    # Cookies sem "sv" (emitidos antes da revogação existir) valem como versão 0.
    if (sessao is None and db.is_configured()) or (
        sessao and int(payload.get("sv") or 0) != sessao["sessao_versao"]
    ):
        g.premium_revoked = True
        return
    g.premium_payload = payload
    g.user_email = email
    g.cliente_id = sessao["id"] if sessao else None


# ── Cobrança criada por este navegador ────────────────────────────────────────

def owns_checkout(req, payment_id: str) -> bool:
    """True se este navegador criou a cobrança (cookie assinado emitido em /checkout/pay).

    Saber o payment_id não basta para liberar o acesso: ele aparece em URLs
    (/checkout/pending?payment_id=...) e no histórico do navegador.
    """
    token = req.cookies.get(CHECKOUT_COOKIE)
    if not token or not payment_id:
        return False
    try:
        data = serializer().loads(token, salt=CHECKOUT_SALT, max_age=CHECKOUT_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(str(data.get("pid", "")), payment_id)


def with_checkout_owner(resp, payment_id: str):
    """Marca este navegador como dono da cobrança (ver owns_checkout)."""
    token = serializer().dumps({"pid": payment_id}, salt=CHECKOUT_SALT)
    return set_signed_cookie(resp, CHECKOUT_COOKIE, token, CHECKOUT_COOKIE_MAX_AGE)


# ── Admin, acesso local e premium ─────────────────────────────────────────────

def is_admin(req) -> bool:
    """Verifica cookie de acesso admin (para testes internos)."""
    token = req.cookies.get(ADMIN_COOKIE)
    if not token:
        return False
    try:
        serializer().loads(token, salt=ADMIN_SALT, max_age=ADMIN_COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def is_local_dev_request(req) -> bool:
    """Desenvolvimento local: acesso liberado sem plano.

    Só vale fora da Vercel e de produção (config.is_local_runtime) e para
    requisições vindas da própria máquina — nunca pelo cabeçalho Host, que
    pode ser forjado. LOCAL_PREMIUM_BYPASS=0 desativa (útil para testar o
    fluxo de planos localmente).
    """
    if current_app.testing or not config.is_local_runtime():
        return False
    if os.environ.get("LOCAL_PREMIUM_BYPASS", "1").strip().lower() in {"0", "false", "no"}:
        return False
    return (req.remote_addr or "") in _LOOPBACK_ADDRS


def is_premium(req) -> bool:
    # Acesso admin também passa pela verificação de premium
    if is_admin(req):
        return True
    if is_local_dev_request(req):
        return True
    token = req.cookies.get(PREMIUM_COOKIE)
    if not token or verify_premium_token(token) is None:
        return False
    # Cookie válido na assinatura, mas revogado (troca de senha, estorno) — ver load_premium_session().
    return not g.get("premium_revoked", False)


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
            flash(Markup(
                'Para analisar exames, <a href="/planos" style="font-weight:700;">contrate um plano</a>.'
            ), "error")
            return redirect(url_for("analysis.index"))
        return f(*args, **kwargs)
    return decorated
