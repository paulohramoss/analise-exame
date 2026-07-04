"""
Testes do endpoint /health e dos helpers de conectividade que ele usa
(core.jobs.health_check_redis, core.db.health_check).
"""

import app as flask_app_module
from core import db, jobs


def test_health_ok_when_no_optional_dependency_configured(client, monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "")

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["checks"] == {"redis": "disabled", "supabase": "disabled"}
    assert body["build_version"] == flask_app_module.BUILD_VERSION


def test_health_reports_redis_down_and_returns_503(client, monkeypatch):
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "redis://fake-host-nao-existe:6379/0")
    monkeypatch.setattr(jobs, "health_check_redis", lambda: False)
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "")

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "down"


def test_health_reports_redis_ok(client, monkeypatch):
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setattr(jobs, "health_check_redis", lambda: True)
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "")

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.get_json()["checks"]["redis"] == "ok"


def test_health_reports_supabase_down_and_returns_503(client, monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    monkeypatch.setattr(db, "health_check", lambda: False)

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert body["checks"]["supabase"] == "down"


def test_health_reports_supabase_ok(client, monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    monkeypatch.setattr(db, "health_check", lambda: True)

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.get_json()["checks"]["supabase"] == "ok"


# ── core.jobs.health_check_redis ──────────────────────────────────────────────

def test_jobs_health_check_redis_false_when_not_configured(monkeypatch):
    monkeypatch.delenv("ANALYSIS_QUEUE_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    assert jobs.health_check_redis() is False


def test_jobs_health_check_redis_false_when_ping_fails(monkeypatch):
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "redis://localhost:1/0")

    class _FakeConn:
        def ping(self):
            raise ConnectionError("boom")

    monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeConn())
    assert jobs.health_check_redis() is False


def test_jobs_health_check_redis_true_when_ping_succeeds(monkeypatch):
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "redis://localhost:1/0")

    class _FakeConn:
        def ping(self):
            return True

    monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeConn())
    assert jobs.health_check_redis() is True


# ── core.db.health_check ──────────────────────────────────────────────────────

def test_db_health_check_false_when_not_configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "")
    db._get_client.cache_clear()
    assert db.health_check() is False


def test_db_health_check_true_when_query_succeeds(monkeypatch):
    class _FakeQuery:
        def select(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            return None

    class _FakeClient:
        def table(self, name):
            return _FakeQuery()

    monkeypatch.setattr(db, "_get_client", lambda: _FakeClient())
    assert db.health_check() is True


def test_db_health_check_false_when_query_raises(monkeypatch):
    class _FakeClient:
        def table(self, name):
            raise ConnectionError("boom")

    monkeypatch.setattr(db, "_get_client", lambda: _FakeClient())
    assert db.health_check() is False
