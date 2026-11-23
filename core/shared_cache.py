"""
Cache compartilhado entre processos (Redis), atrás do cache em memória.

Na Vercel cada instância tem a própria memória: o TTLCache por processo quase
nunca acerta. Com Redis configurado (o mesmo da fila, ver core/jobs), o
resultado de uma análise e as consultas ao PubMed ficam disponíveis para todas
as instâncias e o worker — reenviar o mesmo arquivo não paga a IA de novo.

Sem Redis, tudo vira no-op e o app segue só com o cache em memória. Qualquer
falha do Redis também: cache nunca derruba a análise.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_PREFIX = os.environ.get("SHARED_CACHE_PREFIX", "threehealth:cache:")


def _enabled() -> bool:
    return os.environ.get("SHARED_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _conn():
    if not _enabled():
        return None
    from core import jobs

    return jobs._get_redis_connection()


def get(namespace: str, key: str):
    conn = _conn()
    if conn is None:
        return None
    try:
        raw = conn.get(f"{_PREFIX}{namespace}:{key}")
        return json.loads(raw) if raw else None
    except Exception:
        logger.warning("Cache compartilhado indisponível (leitura).", exc_info=True)
        return None


def set(namespace: str, key: str, value, ttl_seconds: int) -> bool:  # noqa: A001 - API simétrica a get()
    conn = _conn()
    if conn is None or ttl_seconds <= 0:
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        logger.info("Valor não serializável: fora do cache compartilhado (%s).", namespace)
        return False
    try:
        conn.set(f"{_PREFIX}{namespace}:{key}", payload, ex=int(ttl_seconds))
        return True
    except Exception:
        logger.warning("Cache compartilhado indisponível (escrita).", exc_info=True)
        return False
