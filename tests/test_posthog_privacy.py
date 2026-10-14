"""
Garantias da integração PostHog (web/posthog.py): desligada sem configuração e
sem enviar dado de saúde, e-mail ou conteúdo das chamadas de IA.
"""

import io
from unittest.mock import MagicMock

import pytest
from posthog.ai.utils import with_privacy_mode
from werkzeug.security import generate_password_hash

from core import analyzer, db
from web import config, posthog as posthog_module, security


@pytest.fixture()
def fresh_posthog(monkeypatch):
    monkeypatch.setattr(posthog_module, "posthog_client", None)
    monkeypatch.setattr(posthog_module, "_warned_missing_config", False)
    return posthog_module


def test_missing_config_disables_without_breaking(fresh_posthog, monkeypatch):
    monkeypatch.delenv("POSTHOG_PROJECT_TOKEN", raising=False)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    monkeypatch.setattr(config, "is_production_like", lambda: False)
    assert fresh_posthog.init_posthog() is None  # antes: RuntimeError fora de produção


def test_client_is_created_in_privacy_mode(fresh_posthog, monkeypatch):
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_teste")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    created = MagicMock()
    monkeypatch.setattr(fresh_posthog, "Posthog", created)
    monkeypatch.setattr(fresh_posthog.atexit, "register", lambda fn: None)

    fresh_posthog.init_posthog()

    kwargs = created.call_args.kwargs
    assert kwargs["privacy_mode"] is True
    assert kwargs["capture_exception_code_variables"] is False


def test_privacy_mode_hides_ai_input_and_output():
    client = MagicMock(privacy_mode=True)
    assert with_privacy_mode(client, False, "Paciente com rotura do LCA") is None


class _RecordingPosthog:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return MagicMock()
        return _record


def _everything_sent(recorder) -> str:
    return repr(recorder.calls)


def test_login_identifies_by_uuid_only(client, monkeypatch):
    recorder = _RecordingPosthog()
    monkeypatch.setattr(posthog_module, "posthog_client", recorder)
    monkeypatch.setattr(db, "buscar_pagamento_confirmado_por_email", lambda email, **_: ("pay_1", f"{email}|anual"))
    monkeypatch.setattr(db, "buscar_senha_hash_cliente",
                        lambda email: generate_password_hash("senha-certa"))
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: {"id": "uuid-cliente-1", "sessao_versao": 0})
    monkeypatch.setattr(db, "salvar_sessao", MagicMock())

    client.post("/login", data={"email": "medica@clinica.com.br", "senha": "senha-certa"})

    sent = _everything_sent(recorder)
    assert "uuid-cliente-1" in sent
    assert "medica@clinica.com.br" not in sent


def test_analysis_events_carry_no_clinical_text(client, monkeypatch):
    recorder = _RecordingPosthog()
    monkeypatch.setattr(posthog_module, "posthog_client", recorder)
    monkeypatch.setattr(posthog_module, "init_posthog", lambda: recorder)
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", MagicMock(return_value={
        "exam_type": "joelho", "analysis": "Rotura completa do LCA", "references_used": 0,
        "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [],
    }))
    client.get(f"/admin/entrar?key={config.get_admin_key()}")

    client.post(
        "/analyze",
        data={"exam_images": (io.BytesIO(b"fake"), "joelho.jpg"),
              "description": "dor no joelho após queda de moto", "privacy_consent": "accepted"},
        content_type="multipart/form-data",
    )

    events = [c for c in recorder.calls if c[0] == "capture"]
    assert events, "o envio da análise deveria gerar um evento"
    sent = _everything_sent(recorder)
    assert "queda de moto" not in sent and "Rotura completa" not in sent


def test_premium_token_still_issued_without_posthog(client, monkeypatch):
    monkeypatch.setattr(posthog_module, "posthog_client", None)
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: {"id": "c1", "sessao_versao": 0})
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))
    assert client.get("/laudos").status_code == 200
