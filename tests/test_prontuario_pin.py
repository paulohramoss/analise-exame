"""Chave do prontuário fixada no banco: chave trocada por engano bloqueia a gravação."""

import pytest
from cryptography.fernet import Fernet

from core import db, patient_crypto
from e2e.fake_supabase import FakeSupabase

CPF = "52998224725"


@pytest.fixture()
def banco(monkeypatch):
    monkeypatch.setenv("PRONTUARIO_PIN_ENABLED", "1")
    monkeypatch.delenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS", raising=False)
    monkeypatch.delenv("PRONTUARIO_KEY_FINGERPRINT", raising=False)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    fake = FakeSupabase()
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    db._pin_cache.clear()
    yield fake
    db._pin_cache.clear()


def _fixada(banco):
    rows = banco.tables.get("config_seguranca", [])
    return rows[0]["valor"] if rows else None


def test_primeira_gravacao_fixa_a_chave(banco):
    paciente_id, erro = db.criar_paciente("c1", "Maria", CPF)
    assert erro is None and paciente_id
    assert _fixada(banco) == patient_crypto.fingerprint()


def test_chave_trocada_por_engano_bloqueia(banco, monkeypatch):
    db.criar_paciente("c1", "Maria", CPF)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    db._pin_cache.clear()
    assert db.estado_chave_fixada()["estado"] == "divergente"
    assert db.criar_paciente("c1", "José", "11144477735") == (None, db.PACIENTE_INDISPONIVEL)
    assert len(banco.tables["pacientes"]) == 1


def test_rotacao_legitima_atualiza_a_chave_fixada(banco, monkeypatch):
    antiga = patient_crypto._key_bytes().decode()
    db.criar_paciente("c1", "Maria", CPF)
    nova = Fernet.generate_key().decode()
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS", antiga)
    db._pin_cache.clear()

    assert db.estado_chave_fixada()["estado"] == "atualizada"
    assert _fixada(banco) == patient_crypto.fingerprint(nova)
    assert db.criar_paciente("c1", "José", "11144477735")[1] is None


def test_nao_fixa_chave_que_nao_abre_os_pacientes_existentes(banco, monkeypatch):
    banco.tables["pacientes"] = [{"id": "p1", "nome_cifrado": Fernet(Fernet.generate_key()).encrypt(b"Ana").decode()}]
    assert db.estado_chave_fixada()["estado"] == "dados_ilegiveis"
    assert _fixada(banco) is None
    assert db.criar_paciente("c1", "Maria", CPF) == (None, db.PACIENTE_INDISPONIVEL)


def test_sem_tabela_nao_trava_o_app(banco, monkeypatch):
    def _quebra(nome):
        if nome == "config_seguranca":
            raise RuntimeError("relation config_seguranca does not exist")
        return FakeSupabase.table(banco, nome)

    monkeypatch.setattr(banco, "table", _quebra)
    assert db.estado_chave_fixada()["estado"] == "indisponivel"
    assert db.criar_paciente("c1", "Maria", CPF)[1] is None


def test_resultado_fica_em_cache(banco, monkeypatch):
    db.criar_paciente("c1", "Maria", CPF)
    chamadas = []
    original = db.estado_chave_fixada
    monkeypatch.setattr(db, "estado_chave_fixada", lambda: chamadas.append(1) or original())
    db.criar_paciente("c1", "José", "11144477735")
    assert chamadas == []
