"""
Configuração compartilhada dos testes.

Define variáveis de ambiente de teste ANTES de importar `app` (que chama
load_dotenv() na importação) para garantir que segredos reais do .env local
nunca sejam usados nem que testes façam chamadas de rede reais ao Supabase
ou ao Asaas.
"""

import os

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("ADMIN_KEY", "test-admin-key")
os.environ.setdefault("ASAAS_API_KEY", "test-asaas-key")
os.environ.setdefault("ASAAS_WEBHOOK_TOKEN", "")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "")
os.environ.setdefault("SENTRY_DSN", "")

import fakeredis
import pytest

import app as flask_app_module
from core import analyzer, cost_tracking, jobs


@pytest.fixture()
def client():
    flask_app_module.app.testing = True
    with flask_app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_module_level_caches():
    """Evita que o cache de análise e os contadores de gasto vazem entre testes."""
    analyzer._analysis_result_cache.clear()
    with cost_tracking._memory_lock:
        cost_tracking._memory_spend.clear()
    yield
    analyzer._analysis_result_cache.clear()
    with cost_tracking._memory_lock:
        cost_tracking._memory_spend.clear()


@pytest.fixture()
def fake_redis_conn(monkeypatch):
    """Substitui a conexão Redis de core.jobs por uma instância fakeredis isolada."""
    conn = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(jobs, "_redis_conn", conn)
    monkeypatch.setattr(jobs, "_redis_conn_checked", True)
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "redis://fake")
    return conn


@pytest.fixture()
def run_pending_jobs():
    """Retorna uma função que processa todos os jobs pendentes na fila (worker síncrono, burst mode)."""
    import rq
    from rq.worker import SimpleWorker

    def _run(conn) -> None:
        queue = rq.Queue(jobs._QUEUE_NAME, connection=conn)
        SimpleWorker([queue], connection=conn).work(burst=True)

    return _run
