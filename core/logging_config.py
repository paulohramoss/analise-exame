"""
Configuração central de logging estruturado (JSON) para a aplicação.

Cada linha de log é um objeto JSON com timestamp, nível, logger, mensagem,
trace_id (correlaciona todos os logs de uma mesma requisição) e, quando
aplicável, o traceback da exceção. Isso substitui o uso de print() e permite
localizar rapidamente todos os eventos de um laudo específico em produção.
"""

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone

try:
    from flask import g, has_request_context
except ImportError:  # pragma: no cover - flask sempre presente em runtime
    g = None

    def has_request_context() -> bool:
        return False


_RESERVED_RECORD_ATTRS = set(logging.LogRecord(
    "", 0, "", 0, "", None, None
).__dict__.keys()) | {"message", "asctime"}


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


# trace_id fora de requisição HTTP (ex.: job da fila no worker). Permite achar,
# pelo mesmo trace_id, os logs da requisição que enfileirou e os do worker.
_bound_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def bind_trace_id(trace_id: str | None) -> Token:
    """Associa um trace_id ao contexto atual (thread/tarefa). Desfaça com unbind_trace_id."""
    return _bound_trace_id.set(trace_id)


def unbind_trace_id(token: Token) -> None:
    _bound_trace_id.reset(token)


def current_trace_id() -> str | None:
    """trace_id da requisição Flask atual ou, fora dela, o associado por bind_trace_id."""
    if has_request_context() and g is not None:
        trace_id = getattr(g, "trace_id", None)
        if trace_id:
            return trace_id
    return _bound_trace_id.get()


class TraceIdFilter(logging.Filter):
    """Injeta o trace_id da requisição atual (quando existir) em cada registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id()
        return True


class JsonFormatter(logging.Formatter):
    """Formata cada registro de log como uma linha JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
        }

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and key != "trace_id"
        }
        if extras:
            payload["extra"] = extras

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Configura o logger raiz com saída JSON estruturada em stdout.

    Deve ser chamada uma única vez, no início da aplicação. Idempotente:
    chamadas repetidas não duplicam handlers.
    """
    root_logger = logging.getLogger()
    if getattr(root_logger, "_medai_configured", False):
        return

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceIdFilter())

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
    root_logger._medai_configured = True  # type: ignore[attr-defined]


def init_posthog_log_capture() -> bool:
    """Export only records deliberately emitted through the dedicated logger."""
    posthog_logger = logging.getLogger("posthog.logs")
    if getattr(posthog_logger, "_posthog_logs_configured", False):
        return True

    project_token = os.environ.get("POSTHOG_PROJECT_TOKEN", "").strip()
    host = os.environ.get("POSTHOG_HOST", "").strip()
    if not project_token or not host:
        return False

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ImportError:
        logging.getLogger(__name__).warning(
            "PostHog log capture requires opentelemetry-api, opentelemetry-sdk, "
            "and opentelemetry-exporter-otlp."
        )
        return False

    logger_provider = LoggerProvider()
    set_logger_provider(logger_provider)
    exporter = OTLPLogExporter(
        endpoint=f"{host.rstrip('/')}/i/v1/logs",
        headers={"Authorization": f"Bearer {project_token}"},
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    handler = LoggingHandler(logger_provider=logger_provider)
    handler.addFilter(TraceIdFilter())
    posthog_logger.addHandler(handler)
    posthog_logger.setLevel(logging.INFO)
    posthog_logger._posthog_logs_configured = True  # type: ignore[attr-defined]
    return True


def posthog_log_capture_enabled() -> bool:
    """Whether purpose-written PostHog log records can be emitted."""
    return bool(getattr(logging.getLogger("posthog.logs"), "_posthog_logs_configured", False))
