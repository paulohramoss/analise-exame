"""
Worker da fila de análise assíncrona (core/jobs.py).

Uso local:
    python worker.py

Requer as mesmas variáveis de ambiente da aplicação web (GEMINI_API_KEY,
ANTHROPIC_API_KEY, SUPABASE_URL/SUPABASE_SERVICE_KEY) mais um Redis real em
RATELIMIT_STORAGE_URI ou ANALYSIS_QUEUE_REDIS_URL — ver .env.example.

Este é um processo separado do processo web (app.py) e precisa rodar em algo
que sustente processos de longa duração (uma VM, um serviço always-on tipo
Railway/Render, um container) — não funciona hospedado como função
serverless da Vercel, que não mantém processos em background entre
invocações. O endpoint web continua funcionando sem este worker rodando
(cai no caminho síncrono existente), mas as análises enfileiradas não serão
processadas enquanto não houver nenhum worker consumindo a fila.
"""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from core import jobs
from core.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("worker")


def main() -> None:
    if not jobs.is_async_enabled():
        logger.error(
            "Nenhum Redis configurado para a fila (RATELIMIT_STORAGE_URI ou "
            "ANALYSIS_QUEUE_REDIS_URL). Configure um Redis real antes de iniciar o worker."
        )
        sys.exit(1)

    queue = jobs.get_queue()
    if queue is None:
        logger.error("Falha ao conectar ao Redis da fila de análise.")
        sys.exit(1)

    import rq

    # SimpleWorker roda os jobs no próprio processo (sem fork). É a opção
    # portável — necessária no Windows, onde os.fork() não existe. Em Linux
    # (produção), o Worker padrão isola cada job num processo filho.
    worker_class = rq.worker.SimpleWorker if os.name == "nt" else rq.Worker
    worker = worker_class([queue], connection=queue.connection)

    logger.info("Worker iniciado (fila=%s, classe=%s)", queue.name, worker_class.__name__)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
