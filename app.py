"""
Aplicação Flask para análise de exames médicos com IA (Gemini + Claude).

Este módulo só monta a aplicação; rotas e regras ficam no pacote web/ (ver
web/__init__.py). `app` é o objeto WSGI usado pela Vercel (api/index.py) e
pelo `python app.py` local.
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask

load_dotenv()

from core.build_info import get_build_version  # noqa: E402
from core.logging_config import configure_logging  # noqa: E402
from web import config, hooks, security, uploads  # noqa: E402
from web.extensions import limiter  # noqa: E402
from web.routes import ALL_BLUEPRINTS  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

BUILD_VERSION = get_build_version()


def _init_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:  # pragma: no cover - dependência instalada em produção via requirements.txt
        logger.warning("SENTRY_DSN definido, mas o pacote sentry-sdk não está instalado.")
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "").strip()
        or ("production" if config.is_production_like() else "development"),
        release=BUILD_VERSION,
        integrations=[FlaskIntegration(), LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
    )
    logger.info("Sentry inicializado (release=%s)", BUILD_VERSION)


def create_app() -> Flask:
    secret_key = os.environ.get("FLASK_SECRET_KEY", config.DEFAULT_FLASK_SECRET_KEY)
    if config.is_production_like() and secret_key == config.DEFAULT_FLASK_SECRET_KEY:
        raise RuntimeError("FLASK_SECRET_KEY deve ser definido com valor seguro em produção.")

    flask_app = Flask(__name__)
    flask_app.secret_key = secret_key
    flask_app.config["UPLOAD_FOLDER"] = str(uploads.UPLOAD_FOLDER)
    flask_app.config["MAX_CONTENT_LENGTH"] = uploads.MAX_CONTENT_LENGTH

    if config.is_local_runtime() and not os.environ.get("ADMIN_KEY", "").strip():
        logger.warning(
            "ADMIN_KEY não definida — acesso admin local liberado via /admin/entrar?key=%s "
            "(chave temporária, gerada nesta execução; defina ADMIN_KEY no .env para uma chave estável).",
            config.GENERATED_LOCAL_ADMIN_KEY,
        )

    security.init_app(flask_app)
    if limiter is not None:
        limiter.init_app(flask_app)
    hooks.register(flask_app, BUILD_VERSION)
    for blueprint in ALL_BLUEPRINTS:
        flask_app.register_blueprint(blueprint)
    return flask_app


_init_sentry()
app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
