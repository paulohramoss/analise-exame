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
from core.build_info import get_build_version
from core.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("worker")

BUILD_VERSION = get_build_version()

# Mesmo processo web (app.py) reporta pro Sentry com release=BUILD_VERSION;
# o worker é um processo separado (Railway/Render) e sem isso, exceções nos
# jobs de análise (core/jobs.py:run_analysis_job) não apareciam em lugar
# nenhum nem eram correlacionadas ao commit que as introduziu.
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.rq import RqIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "").strip() or "production",
            release=BUILD_VERSION,
            integrations=[
                RqIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
            send_default_pii=False,
        )
        logger.info("Sentry inicializado no worker (release=%s)", BUILD_VERSION)
    except ImportError:  # pragma: no cover - dependência instalada em produção via requirements.txt
        logger.warning("SENTRY_DSN definido, mas o pacote sentry-sdk não está instalado.")


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
