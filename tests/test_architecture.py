"""
Testes da reorganização em módulos: cache com TTL/limite, cache da versão de
sessão, log de acesso assíncrono, mensagens de erro da IA e persistência única.
"""

import time
from unittest.mock import MagicMock

import app as flask_app_module
from core import db
from core.ttl_cache import TTLCache
from web import access_log, presentation, security


# ── TTLCache ──────────────────────────────────────────────────────────────────

def test_ttl_cache_evicts_least_recently_used():
    cache = TTLCache(maxsize=2, ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")          # "a" passa a ser o mais recente
    cache.set("c", 3)       # estoura o limite: sai "b"
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert len(cache) == 2


def test_ttl_cache_expires_entries():
    cache = TTLCache(maxsize=10, ttl_seconds=0.01)
    cache.set("a", 1)
    time.sleep(0.02)
    assert cache.get("a") is None
    assert "a" not in cache


def test_ttl_cache_can_store_none_with_sentinel_default():
    cache = TTLCache(maxsize=10, ttl_seconds=60)
    missing = object()
    cache.set("caso", None)
    assert cache.get("caso", missing) is None
    assert cache.get("outro", missing) is missing


# ── Cache da versão de sessão ─────────────────────────────────────────────────

def test_session_version_is_cached_between_requests(client, monkeypatch):
    monkeypatch.setattr(security, "_session_cache", TTLCache(maxsize=10, ttl_seconds=60))
    lookup = MagicMock(return_value={"id": "c1", "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_sessao_cliente", lookup)
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "cliente@example.com", 3600))

    for _ in range(3):
        assert client.get("/laudos").status_code == 200

    assert lookup.call_count == 1  # antes: uma consulta ao Supabase por requisição


def test_revocation_in_same_process_is_seen_immediately(client, monkeypatch):
    monkeypatch.setattr(security, "_session_cache", TTLCache(maxsize=10, ttl_seconds=60))
    version = {"sv": 0}
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: {"id": "c1", "sessao_versao": version["sv"]})

    def _revogar(email):
        version["sv"] += 1
        return True

    monkeypatch.setattr(db, "revogar_sessoes_cliente", _revogar)
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "cliente@example.com", 3600))
    assert client.get("/laudos").status_code == 200

    security.revoke_sessions("cliente@example.com")

    assert client.get("/laudos").status_code == 302


# ── Log de acesso ─────────────────────────────────────────────────────────────

def test_access_log_async_writes_in_background(monkeypatch):
    monkeypatch.setenv("ACCESS_LOG_ASYNC", "1")
    salvar = MagicMock()
    monkeypatch.setattr(db, "salvar_log", salvar)

    access_log.record(endpoint="/laudos", metodo="GET", status_code=200)

    assert access_log.flush(timeout=2)
    salvar.assert_called_once_with(endpoint="/laudos", metodo="GET", status_code=200)


def test_status_polling_and_health_are_not_logged(client, monkeypatch, fake_redis_conn):
    salvar = MagicMock()
    monkeypatch.setattr(db, "salvar_log", salvar)

    client.get("/health")
    client.get("/trial/analyze/status/job-x")
    assert salvar.call_count == 0

    client.get("/planos")
    assert salvar.call_count == 1


# ── Mensagens de erro da IA ───────────────────────────────────────────────────

class _ApiError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def test_ai_error_response_maps_known_failures(monkeypatch):
    monkeypatch.setattr(presentation.config, "is_local_runtime", lambda: False)
    assert presentation.ai_error_response(_ApiError("RESOURCE_EXHAUSTED quota"))[1] == 429
    assert presentation.ai_error_response(_ApiError("model overloaded", 503))[1] == 503
    message, status = presentation.ai_error_response(_ApiError("API key not valid"))
    assert status == 503 and "GEMINI_API_KEY" not in message  # config só aparece em ambiente local
    assert presentation.ai_error_response(RuntimeError("boom")) == (
        "Não foi possível processar o exame. Tente novamente.", 500
    )


# ── Persistência única da análise ─────────────────────────────────────────────

def test_salvar_analise_com_imagens_persists_each_image(monkeypatch):
    salvar_analise = MagicMock(return_value="analise-1")
    salvar_imagem = MagicMock()
    monkeypatch.setattr(db, "salvar_analise", salvar_analise)
    monkeypatch.setattr(db, "salvar_imagem_exame", salvar_imagem)
    result = {"exam_type": "joelho", "analysis": "laudo", "model_used": "m", "references_used": 1, "num_images": 2}

    analise_id = db.salvar_analise_com_imagens(
        result, "premium",
        images=[(b"a", "image/png"), (b"bb", "image/jpeg")],
        image_meta=[{"origem": "dicom", "arquivo_original": "x.dcm"}, {}],
        tempo_ms=10, descricao_usuario="dor",
    )

    assert analise_id == "analise-1"
    assert salvar_analise.call_args.kwargs["descricao_usuario"] == "dor"
    assert [c.kwargs["ordem"] for c in salvar_imagem.call_args_list] == [1, 2]
    assert salvar_imagem.call_args_list[0].kwargs["origem"] == "dicom"
    assert salvar_imagem.call_args_list[1].kwargs["tamanho_bytes"] == 2


# ── Montagem da aplicação ─────────────────────────────────────────────────────

def test_all_routes_are_registered_in_blueprints():
    endpoints = {rule.endpoint for rule in flask_app_module.app.url_map.iter_rules()} - {"static"}
    assert len(endpoints) == 43
    assert all(endpoint.split(".")[0] in {"analysis", "laudos", "pacientes", "billing", "auth", "account"} for endpoint in endpoints)
