"""Rotação da chave do prontuário: nada fica ilegível durante a troca."""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from core import db, patient_crypto
from scripts import prontuario_chaves

CPF = "52998224725"


class _Tabela:
    """Tabela 'pacientes' em memória com o subconjunto da API usado pelo script."""

    def __init__(self, linhas):
        self.linhas = linhas
        self._range = None
        self._update = None
        self._id = None

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def update(self, payload):
        self._update = payload
        return self

    def eq(self, col, val):
        self._id = val
        return self

    def execute(self):
        if self._update is not None:
            for linha in self.linhas:
                if linha["id"] == self._id:
                    linha.update(self._update)
            self._update = None
            return SimpleNamespace(data=[])
        a, b = self._range
        return SimpleNamespace(data=self.linhas[a:b + 1])


class _Cliente:
    def __init__(self, linhas):
        self.tabela = _Tabela(linhas)

    def table(self, _nome):
        return self.tabela


@pytest.fixture()
def chaves(monkeypatch):
    antiga, nova = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", antiga)
    monkeypatch.delenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS", raising=False)
    return antiga, nova


def _linha(id_, nome):
    return {"id": id_, "nome_cifrado": patient_crypto.encrypt(nome), "cpf_cifrado": patient_crypto.encrypt(CPF)}


def test_chave_antiga_continua_abrindo_depois_da_troca(monkeypatch, chaves):
    antiga, nova = chaves
    token = patient_crypto.encrypt("Maria")
    hash_antigo = patient_crypto.cpf_hash(CPF)

    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS", antiga)

    assert patient_crypto.decrypt(token) == "Maria"
    assert not patient_crypto.opens_with_primary(token)
    assert patient_crypto.opens_with_primary(patient_crypto.encrypt("Ana"))
    assert hash_antigo in patient_crypto.cpf_hash_candidates(CPF)
    assert patient_crypto.cpf_hash(CPF) != hash_antigo


def test_duplicado_detectado_com_hash_da_chave_antiga(monkeypatch, chaves):
    antiga, nova = chaves
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS", antiga)
    calls = []

    class _Q:
        def __getattr__(self, name):
            def rec(*a, **k):
                calls.append((name, a))
                return self
            return rec

        def execute(self):
            return SimpleNamespace(data=[{"id": "x"}])

    monkeypatch.setattr(db, "_get_client", lambda: SimpleNamespace(table=lambda _n: _Q()))
    assert db.criar_paciente("c1", "Maria", CPF) == (None, db.PACIENTE_DUPLICADO)
    in_call = next(a for n, a in calls if n == "in_")
    assert in_call[0] == "cpf_hash" and len(in_call[1]) == 2


def test_fingerprint_estavel_e_nao_revela_chave(chaves):
    antiga, _ = chaves
    fp = patient_crypto.fingerprint()
    assert fp == patient_crypto.fingerprint(antiga) and len(fp) == 16
    assert antiga[:10] not in fp


def test_verificar_e_rotacionar(monkeypatch, chaves):
    antiga, nova = chaves
    linhas = [_linha("p1", "Maria"), _linha("p2", "José")]
    cliente = _Cliente(linhas)

    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS", antiga)
    linhas.append(_linha("p3", "Ana"))  # já cifrado com a nova

    s = prontuario_chaves.verificar(cliente)
    assert (s["total"], s["atual"], s["antiga"], s["nenhuma"]) == (3, 1, 2, 0)

    simulado = prontuario_chaves.rotacionar(cliente, aplicar=False)
    assert simulado["recifrados"] == 2
    assert prontuario_chaves.verificar(cliente)["antiga"] == 2  # simulação não grava

    feito = prontuario_chaves.rotacionar(cliente, aplicar=True)
    assert (feito["recifrados"], feito["ja_atuais"], feito["falhas"]) == (2, 1, [])

    # Agora a chave antiga pode sair: tudo abre só com a nova, e o hash é o da nova.
    monkeypatch.delenv("PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS")
    s = prontuario_chaves.verificar(cliente)
    assert (s["atual"], s["antiga"], s["nenhuma"]) == (3, 0, 0)
    assert patient_crypto.decrypt(linhas[0]["nome_cifrado"]) == "Maria"
    assert linhas[0]["cpf_hash"] == patient_crypto.cpf_hash(CPF)


def test_verificar_acusa_prontuario_ilegivel(monkeypatch, chaves):
    _, nova = chaves
    cliente = _Cliente([_linha("p1", "Maria")])
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)  # antiga perdida
    s = prontuario_chaves.verificar(cliente)
    assert s["ilegiveis"] == ["p1"]
    monkeypatch.setattr(db, "_get_client", lambda: cliente)
    assert prontuario_chaves.main(["verificar"]) == 1
    assert prontuario_chaves.rotacionar(cliente, aplicar=True)["falhas"] == ["p1"]


def test_chave_errada_com_impressao_digital_bloqueia_prontuario(monkeypatch, chaves):
    antiga, nova = chaves
    monkeypatch.setenv("PRONTUARIO_KEY_FINGERPRINT", patient_crypto.fingerprint(antiga))
    assert patient_crypto.is_configured()
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)
    assert not patient_crypto.is_configured()
    assert db.criar_paciente("c1", "Maria", CPF) == (None, db.PACIENTE_INDISPONIVEL)


def test_painel_admin_mostra_impressao_digital(client, monkeypatch, chaves):
    from web import config

    antiga, nova = chaves
    client.get(f"/admin/entrar?key={config.get_admin_key()}")
    html = client.get("/admin/custos").get_data(as_text=True)
    assert patient_crypto.fingerprint(antiga) in html and antiga not in html
    assert "PRONTUARIO_KEY_FINGERPRINT na Vercel é opcional" in html

    monkeypatch.setenv("PRONTUARIO_KEY_FINGERPRINT", patient_crypto.fingerprint(antiga))
    assert "Confere com PRONTUARIO_KEY_FINGERPRINT" in client.get("/admin/custos").get_data(as_text=True)
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", nova)
    assert "NÃO confere" in client.get("/admin/custos").get_data(as_text=True)
