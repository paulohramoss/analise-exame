"""
Autorização entre clientes: o cliente A nunca lê, valida, comenta, exporta ou
exclui o que é do cliente B. Cada rota precisa repassar o cliente_id de QUEM
ESTÁ LOGADO (e include_all=False) para a consulta ao banco — o filtro em si é
coberto em test_db_queries.
"""

from unittest.mock import MagicMock

import pytest
from werkzeug.security import generate_password_hash

from core import db
from web import config, security


class _FakeDb:
    """Laudos e contas de dois clientes; buscar_analise filtra por cliente como o Supabase faria."""

    ANALISES = {
        "laudo-a": {"id": "laudo-a", "cliente_id": "cliente-a", "analise_completa": "laudo A", "tipo_exame": "joelho"},
        "laudo-b": {"id": "laudo-b", "cliente_id": "cliente-b", "analise_completa": "laudo B", "tipo_exame": "ombro"},
    }
    CLIENTES = {"a@example.com": "cliente-a", "b@example.com": "cliente-b"}

    def __init__(self):
        self.calls = []

    def buscar_analise(self, analise_id, cliente_id=None, include_all=False):
        self.calls.append(("buscar_analise", analise_id, cliente_id, include_all))
        row = self.ANALISES.get(analise_id)
        if row and (include_all or row["cliente_id"] == cliente_id):
            return row
        return None

    def listar_analises(self, cliente_id=None, limit=50, include_all=False):
        self.calls.append(("listar_analises", cliente_id, include_all))
        return [r for r in self.ANALISES.values() if include_all or r["cliente_id"] == cliente_id]

    def buscar_sessao_cliente(self, email):
        cliente_id = self.CLIENTES.get(email)
        return {"id": cliente_id, "sessao_versao": 0} if cliente_id else None


@pytest.fixture()
def fake_db(monkeypatch):
    fake = _FakeDb()
    for name in ("buscar_analise", "listar_analises", "buscar_sessao_cliente"):
        monkeypatch.setattr(db, name, getattr(fake, name))
    monkeypatch.setattr(db, "listar_validacoes_clinicas", lambda ids: [])
    monkeypatch.setattr(db, "listar_feedbacks_por_analises", lambda ids: [])
    return fake


def _login_as(client, email):
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", email, 3600))


def test_client_cannot_open_another_clients_report(client, fake_db):
    _login_as(client, "a@example.com")

    assert client.get("/laudos/laudo-a").status_code == 200
    resp = client.get("/laudos/laudo-b")

    assert resp.status_code == 302 and "/laudos" in resp.headers["Location"]
    assert ("buscar_analise", "laudo-b", "cliente-a", False) in fake_db.calls


def test_history_and_dashboard_only_list_own_reports(client, fake_db):
    _login_as(client, "a@example.com")

    html = client.get("/laudos").get_data(as_text=True)
    client.get("/clinica/dashboard")

    assert "laudo-a" in html and "laudo-b" not in html
    assert all(call == ("listar_analises", "cliente-a", False) for call in fake_db.calls if call[0] == "listar_analises")


@pytest.mark.parametrize("path,payload", [
    ("/feedback", {"analysis_id": "laudo-b", "feedback": "agree"}),
    ("/validacao-clinica", {"analysis_id": "laudo-b", "final_diagnosis": "parecer"}),
])
def test_client_cannot_validate_or_comment_another_clients_report(client, fake_db, monkeypatch, path, payload):
    salvar_feedback = MagicMock(return_value="f1")
    salvar_validacao = MagicMock(return_value="v1")
    monkeypatch.setattr(db, "salvar_feedback", salvar_feedback)
    monkeypatch.setattr(db, "salvar_diagnostico_validado", salvar_validacao)
    _login_as(client, "a@example.com")

    resp = client.post(path, json=payload)

    assert resp.status_code == 404
    salvar_feedback.assert_not_called()
    salvar_validacao.assert_not_called()


def test_account_export_and_delete_use_own_client_id(client, fake_db, monkeypatch):
    exportar = MagicMock(return_value={"cliente": {"id": "cliente-a"}})
    excluir = MagicMock(return_value=True)
    monkeypatch.setattr(db, "exportar_dados_cliente", exportar)
    monkeypatch.setattr(db, "excluir_dados_cliente", excluir)
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda email: generate_password_hash("senha-a"))
    _login_as(client, "a@example.com")

    client.get("/conta/dados.json")
    client.post("/conta/excluir", data={"senha": "senha-a", "confirmacao": "EXCLUIR"})

    exportar.assert_called_once_with("cliente-a")
    excluir.assert_called_once_with("cliente-a")


def test_admin_sees_every_report(client, fake_db):
    client.get(f"/admin/entrar?key={config.get_admin_key()}")

    assert client.get("/laudos/laudo-b").status_code == 200
    assert ("buscar_analise", "laudo-b", None, True) in fake_db.calls


def test_buscar_analise_filters_by_client_in_the_query(monkeypatch):
    """A rota repassa o cliente; a consulta precisa de fato filtrar por ele."""
    calls = []

    class _Query:
        def __getattr__(self, name):
            def _record(*args, **kwargs):
                calls.append((name, args))
                return self
            return _record

        def execute(self):
            return MagicMock(data=[])

    fake_client = MagicMock()
    fake_client.table.return_value = _Query()
    monkeypatch.setattr(db, "_get_client", lambda: fake_client)

    db.buscar_analise("laudo-b", cliente_id="cliente-a")
    assert ("eq", ("cliente_id", "cliente-a")) in calls

    calls.clear()
    assert db.buscar_analise("laudo-b") is None  # sem cliente e sem admin: nem consulta
    assert calls == []
