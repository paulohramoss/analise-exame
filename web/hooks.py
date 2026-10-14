"""Hooks de requisição: sessão, CSRF por origem, cabeçalhos, log de acesso e Jinja."""

import hashlib
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, flash, g, jsonify, make_response, redirect, request, url_for

from core.logging_config import new_trace_id, posthog_log_capture_enabled
from web import access_log, presentation, security
from web.extensions import RateLimitExceeded

logger = logging.getLogger(__name__)
posthog_logger = logging.getLogger("posthog.logs")

# Rotas que recebem POST de fora do navegador do usuário: autenticam por
# cabeçalho próprio (X-API-Key, token do Asaas), não por cookie — CSRF não se aplica.
_CSRF_EXEMPT_PATHS = {"/webhook/asaas", "/api/analyze"}

# Sem sessão, sem log de acesso: health check do monitor e o polling de status
# (uma requisição a cada 2s por análise em andamento) só gerariam ruído.
_QUIET_PATH_PREFIXES = ("/static", "/health")
_QUIET_PATH_MARKERS = ("/status",)


def _is_quiet(path: str) -> bool:
    return path.startswith(_QUIET_PATH_PREFIXES) or any(marker in path for marker in _QUIET_PATH_MARKERS)


def _host_of(url: str) -> str:
    return urlsplit(url.strip()).netloc.lower()


def _trusted_hosts() -> set[str]:
    """Hosts (host[:porta]) aceitos como origem de POST.

    Compara só o host, não o esquema: atrás do proxy da Vercel o Flask pode ver
    http enquanto o navegador manda Origin https — comparar o esquema recusaria
    todo POST legítimo em produção.
    """
    hosts = {request.host.lower()}
    candidates = [os.environ.get("APP_BASE_URL", "")] + os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    hosts.update(_host_of(c) for c in candidates if c.strip())
    return hosts


def _frame_ancestors_for(path: str) -> str:
    # /trial é embutido via iframe na landing page (WordPress). TRIAL_FRAME_ANCESTORS
    # lista quem pode embutir (ex.: "https://threehealth.com.br"); sem a variável,
    # o /trial continua embutível em qualquer site, como antes.
    if path == "/trial":
        allowed = os.environ.get("TRIAL_FRAME_ANCESTORS", "").replace(",", " ").split()
        return " ".join(["'self'", *allowed]) if allowed else "*"
    return "'self'"


def register(app: Flask, build_version: str) -> None:
    @app.before_request
    def start_request():
        g.t0 = time.time()
        g.trace_id = new_trace_id()
        g.analise_id = None
        g.cliente_id = None
        g.user_email = None
        g.is_admin = False
        g.premium_payload = None
        g.premium_revoked = False
        g.modo = None

        if request.path.startswith(("/static", "/health")):
            return
        g.is_admin = security.is_admin(request)
        if not g.is_admin:
            security.load_premium_session()

        # Flask has no PostHog middleware. Keep one fresh SDK context active for
        # the complete request so later captures and centralized error handling
        # inherit the authenticated customer's stable UUID.
        from web.posthog import posthog_client

        if posthog_client is not None:
            context = posthog_client.new_context(fresh=True)
            context.__enter__()
            g.posthog_context = context

            # An authenticated customer id is authoritative. Browser tracing
            # headers are analytics-only fallback context for future clients.
            distinct_id = str(g.get("cliente_id") or request.headers.get("X-POSTHOG-DISTINCT-ID", "")).strip()
            if distinct_id:
                posthog_client.identify_context(distinct_id)

            session_id = request.headers.get("X-POSTHOG-SESSION-ID", "").strip()
            if session_id:
                posthog_client.set_context_session(session_id)

    @app.teardown_request
    def close_posthog_context(_error):
        """End the request-scoped SDK context after error handlers have run."""
        context = g.pop("posthog_context", None)
        if context is not None:
            context.__exit__(None, None, None)

    @app.before_request
    def csrf_origin_check():
        """Recusa POST/PUT/PATCH/DELETE vindos de outro site (CSRF).

        Os cookies são SameSite=Lax, o que já barra a maioria dos casos; esta
        checagem fecha o resto (subdomínios irmãos, navegadores antigos). Os
        navegadores atuais enviam Origin em todo POST; sem Origin, vale o Referer;
        sem nenhum dos dois (cliente não-navegador), não há cookie de vítima a abusar.
        """
        if request.method in {"GET", "HEAD", "OPTIONS"} or request.path in _CSRF_EXEMPT_PATHS:
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source or _host_of(source) in _trusted_hosts():
            return None
        logger.warning("Requisição recusada por origem não confiável (origin=%s, path=%s)", source, request.path)
        return jsonify({"error": "Origem da requisição não permitida."}), 403

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        # CSP mínima e segura de ativar já: impede clickjacking, <base> injetado e
        # plugins. Restringir script-src exige mover os scripts inline para
        # arquivos/nonce — fica para uma etapa própria.
        response.headers.setdefault(
            "Content-Security-Policy",
            f"frame-ancestors {_frame_ancestors_for(request.path)}; base-uri 'self'; object-src 'none'",
        )
        if security.secure_cookie():
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.after_request
    def set_cache_headers_and_log(response):
        """Impede cache de páginas HTML e agenda o log de acesso no banco."""
        trace_id = g.get("trace_id")
        if trace_id:
            response.headers["X-Trace-Id"] = trace_id

        if "text/html" in response.content_type:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        if not _is_quiet(request.path):
            access_log.record(
                endpoint=request.path,
                metodo=request.method,
                status_code=response.status_code,
                ip_address=security.get_ip(),
                user_agent=request.headers.get("User-Agent"),
                modo=g.get("modo"),
                tempo_ms=int((time.time() - g.get("t0", time.time())) * 1000),
                cliente_id=g.get("cliente_id"),
                analise_id=g.get("analise_id"),
            )
            if posthog_log_capture_enabled():
                route = request.url_rule.rule if request.url_rule is not None else "unmatched"
                posthog_logger.info(
                    "http_request_completed",
                    extra={
                        "http_method": request.method,
                        "http_route": route,
                        "http_status_code": response.status_code,
                    },
                )
        return response

    if RateLimitExceeded is not None:
        @app.errorhandler(RateLimitExceeded)
        def handle_rate_limit(error):
            message = "Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente."
            retry_after = getattr(error, "retry_after", None)
            if request.path.startswith(("/trial", "/api")):
                response = jsonify({"error": message})
                response.status_code = 429
            else:
                flash(message, "error")
                response = make_response(redirect(url_for("analysis.index")), 429)
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response

    _register_jinja(app, build_version)


def _register_jinja(app: Flask, build_version: str) -> None:
    static_hashes: dict[str, str] = {}

    def static_url(filename: str) -> str:
        """URL de um arquivo estático com ?v=<hash do conteúdo>.

        O vercel.json manda o navegador guardar /static/* por 1 ano (immutable);
        o hash muda a URL a cada alteração do arquivo e evita CSS/JS desatualizado.
        """
        version = None if app.debug else static_hashes.get(filename)
        if version is None:
            try:
                content = Path(app.static_folder, filename).read_bytes()
            except OSError:
                return url_for("static", filename=filename)
            version = hashlib.sha256(content).hexdigest()[:10]
            static_hashes[filename] = version
        return url_for("static", filename=filename, v=version)

    app.jinja_env.globals["BUILD_VERSION"] = build_version
    app.jinja_env.globals["static_url"] = static_url
    app.jinja_env.filters["datetime_br"] = presentation.format_datetime_br
    app.jinja_env.filters["exam_label"] = presentation.format_exam_label
