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


def current_trace_id() -> str | None:
    """Retorna o trace_id da requisição Flask atual, se houver uma em andamento."""
    if has_request_context() and g is not None:
        return getattr(g, "trace_id", None)
    return None


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
