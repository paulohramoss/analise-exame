"""clientes.premium_expira é a validade oficial do plano (login e view usam a mesma).

core.db fica sem banco nos testes (ver conftest.py); aqui as consultas são
mockadas para simular o que o banco devolveria.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import db
from web import accounts, config

MIGRATION = Path(__file__).resolve().parent.parent / "supabase_migrations" / "2026-11-17-premium-fonte-unica.sql"


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _pagamento(monkeypatch, plano="anual", inicio=None):
    monkeypatch.setattr(
        db, "buscar_pagamento_confirmado_por_email",
        lambda email, **_: ("pay_1", f"{email}|{plano}", inicio or _iso(timedelta(days=-1))),
    )


def test_validade_vem_do_banco(monkeypatch):
    _pagamento(monkeypatch)
    expira = _iso(timedelta(days=500))  # renovação somada: além de um ciclo do plano
    monkeypatch.setattr(db, "buscar_validade_premium", lambda email: (True, expira))

    plan = accounts.find_active_plan("a@example.com")

    assert plan is not None
    assert abs(plan.valid_until - datetime.fromisoformat(expira).timestamp()) < 1


def test_banco_vencido_bloqueia_mesmo_com_pagamento_recente(monkeypatch):
    _pagamento(monkeypatch, plano="mensal")
    monkeypatch.setattr(db, "buscar_validade_premium", lambda email: (True, _iso(timedelta(days=-1))))

    assert accounts.find_active_plan("a@example.com") is None


def test_premium_desligado_no_banco_bloqueia(monkeypatch):
    """Reembolso: o trigger desliga premium; o login tem que respeitar."""
    _pagamento(monkeypatch)
    monkeypatch.setattr(db, "buscar_validade_premium", lambda email: (False, _iso(timedelta(days=200))))

    assert accounts.find_active_plan("a@example.com") is None


def test_sem_data_no_banco_bloqueia(monkeypatch):
    _pagamento(monkeypatch)
    monkeypatch.setattr(db, "buscar_validade_premium", lambda email: (True, None))

    assert accounts.find_active_plan("a@example.com") is None


def test_banco_indisponivel_cai_no_calculo_pelo_pagamento(monkeypatch):
    inicio = _iso(timedelta(days=-10))
    _pagamento(monkeypatch, plano="mensal", inicio=inicio)
    monkeypatch.setattr(db, "buscar_validade_premium", lambda email: None)

    plan = accounts.find_active_plan("a@example.com")

    esperado = datetime.fromisoformat(inicio).timestamp() + config.PLANS["mensal"]["cookie_max_age"]
    assert plan is not None
    assert abs(plan.valid_until - esperado) < 1
    assert plan.valid_until > time.time()


def test_duracoes_do_trigger_batem_com_config():
    sql = MIGRATION.read_text(encoding="utf-8")
    for plano, dados in config.PLANS.items():
        m = re.search(rf"WHEN '{plano}'\s+THEN INTERVAL '(\d+) days'", sql)
        assert m, f"plano {plano} sem duração no trigger"
        assert int(m.group(1)) * 24 * 3600 == dados["cookie_max_age"], plano
