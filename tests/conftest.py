"""
Configuração compartilhada dos testes.

Define variáveis de ambiente de teste ANTES de importar `app` e desliga o
load_dotenv(): nenhum valor do .env local (segredos reais, flags como
INCLUDE_REFERENCE_PDF) pode vazar para os testes, nem eles podem fazer chamadas
de rede reais ao Supabase ou ao Asaas.
"""

import os

import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False  # antes de qualquer "from dotenv import load_dotenv"

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("ADMIN_KEY", "test-admin-key")
os.environ.setdefault("ASAAS_API_KEY", "test-asaas-key")
os.environ.setdefault("ASAAS_WEBHOOK_TOKEN", "")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "")
os.environ.setdefault("SENTRY_DSN", "")
# Chaves aceitas em X-API-Key pela rota /api/analyze (ver _api_access_key_valid).
os.environ.setdefault("API_ACCESS_KEYS", "chave,chave-do-chamador")
# SMTP desligado: nenhum teste envia e-mail real (testes do /acesso mockam o mailer).
os.environ.setdefault("SMTP_HOST", "")
# Desliga a consulta DNS da validação de e-mail: nenhum teste pode depender de
# rede. Os testes do validador ligam a checagem explicitamente com stub de DNS.
os.environ.setdefault("EMAIL_MX_CHECK", "0")
# Sem cache da versão de sessão: os testes de revogação trocam o valor no
# "banco" entre duas requisições (o cache tem teste próprio).
os.environ.setdefault("SESSION_CACHE_TTL_SECONDS", "0")
# Log de acesso síncrono: nada de thread de fundo tocando no db durante testes
# que substituem o cliente do Supabase (o modo assíncrono tem teste próprio).
os.environ.setdefault("ACCESS_LOG_ASYNC", "0")
# Marcação dos achados nas imagens é uma chamada extra à IA depois do laudo: os
# testes que simulam o modelo não a esperam. Ela tem testes próprios
# (test_annotations.py), que a ligam explicitamente.
os.environ.setdefault("ANNOTATIONS_ENABLED", "0")

import fakeredis
import pytest

import app as flask_app_module
from web import extensions
from core import analyzer, cost_tracking, jobs


@pytest.fixture()
def client():
    flask_app_module.app.testing = True
    with flask_app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_module_level_caches():
    """Evita que o cache de análise, os contadores de gasto e o rate limit vazem entre testes."""
    if extensions.limiter is not None:
        extensions.limiter.reset()
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


@pytest.fixture(autouse=True)
def _block_real_http(monkeypatch):
    """Nenhum teste fala com a rede: um mock esquecido vira erro, não chamada ao Asaas/Wikimedia."""
    import requests.adapters

    def _refuse(self, request, *args, **kwargs):
        raise RuntimeError(f"Teste tentou acessar a rede: {request.method} {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _refuse)
