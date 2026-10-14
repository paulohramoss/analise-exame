"""
Log de acesso (tabela logs_acesso) gravado fora do ciclo da requisição.

Antes, cada requisição — inclusive o polling de status, uma vez a cada 2s por
análise — esperava um INSERT no Supabase antes de responder. Agora a entrada
vai para uma fila em memória e uma thread de fundo grava. Se a fila encher
(banco lento/fora), as entradas novas são descartadas: log de acesso nunca
pode derrubar ou atrasar o atendimento.

ACCESS_LOG_ASYNC=0 grava de forma síncrona (útil em testes e scripts).
"""

import logging
import os
import queue
import threading

from core import db

logger = logging.getLogger(__name__)

_QUEUE_MAX = int(os.environ.get("ACCESS_LOG_QUEUE_MAX", "1000"))
_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_dropped = 0


def _is_async() -> bool:
    return os.environ.get("ACCESS_LOG_ASYNC", "1").strip().lower() not in {"0", "false", "no"}


def _run() -> None:
    while True:
        entry = _queue.get()
        try:
            db.salvar_log(**entry)
        except Exception:  # pragma: no cover - salvar_log já engole erros
            logger.debug("Falha ao gravar log de acesso", exc_info=True)
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="access-log", daemon=True)
            _worker.start()


def record(**entry) -> None:
    """Agenda a gravação de uma linha em logs_acesso (argumentos de db.salvar_log)."""
    global _dropped
    if not _is_async():
        db.salvar_log(**entry)
        return
    _ensure_worker()
    try:
        _queue.put_nowait(entry)
    except queue.Full:
        _dropped += 1
        if _dropped % 100 == 1:
            logger.warning("Fila do log de acesso cheia; %d entradas descartadas até agora.", _dropped)


def flush(timeout: float = 5.0) -> bool:
    """Espera a fila esvaziar (testes / encerramento). True se esvaziou a tempo."""
    done = threading.Event()

    def _wait():
        _queue.join()
        done.set()

    threading.Thread(target=_wait, daemon=True).start()
    return done.wait(timeout)
