"""
Testes das consultas sensíveis de core/db.py com um cliente Supabase falso que
registra a cadeia de chamadas (sem rede).
"""

from types import SimpleNamespace

from core import db


class _FakeQuery:
    def __init__(self, calls, rows):
        self._calls = calls
        self._rows = rows

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return self
        return _record

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _FakeClient:
    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    def table(self, name):
        self.calls.append(("table", (name,), {}))
        return _FakeQuery(self.calls, self._rows)


def _use_fake_client(monkeypatch, rows=None):
    fake = _FakeClient(rows)
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    return fake


def test_pagamento_confirmado_usa_igualdade_exata_sem_curinga(monkeypatch):
    """`%`/`_` digitados no e-mail não podem agir como curinga (antes: ilike 'email%')."""
    fake = _use_fake_client(monkeypatch)

    db.buscar_pagamento_confirmado_por_email("%@%", planos=("mensal", "anual"))

    methods = [name for name, _, _ in fake.calls]
    assert "ilike" not in methods and "like" not in methods
    in_call = next(args for name, args, _ in fake.calls if name == "in_" and args[0] == "external_reference")
    assert in_call[1] == ["%@%", "%@%|mensal", "%@%|anual"]


def test_caso_validado_so_usa_casos_aprovados(monkeypatch):
    fake = _use_fake_client(monkeypatch)
    db._validated_case_cache.clear()

    db.buscar_caso_validado_referencia("joelho")

    assert ("eq", ("aprovado_para_referencia", True), {}) in fake.calls


def test_admin_lista_laudos_de_todos_mesmo_com_conta_propria(monkeypatch):
    """O admin tem cliente_id (prontuário próprio), mas o histórico mostra todos os laudos."""
    fake = _use_fake_client(monkeypatch)

    db.listar_analises(cliente_id="cliente-admin", include_all=True)
    db.buscar_analise("laudo-1", cliente_id="cliente-admin", include_all=True)

    assert not any(name == "eq" and args[0] == "cliente_id" for name, args, _ in fake.calls)


def test_profissional_lista_so_os_proprios_laudos(monkeypatch):
    fake = _use_fake_client(monkeypatch)

    db.listar_analises(cliente_id="cliente-a")
    db.buscar_analise("laudo-1", cliente_id="cliente-a")

    assert [args for name, args, _ in fake.calls if name == "eq" and args[0] == "cliente_id"] == [
        ("cliente_id", "cliente-a"), ("cliente_id", "cliente-a"),
    ]
