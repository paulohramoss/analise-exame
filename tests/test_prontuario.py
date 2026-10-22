"""
Prontuário de pacientes: cifra de nome/CPF, isolamento entre profissionais,
cadastro, tratamentos, evolução, vínculo dos laudos e linha do tempo.

As rotas usam um "banco" em memória que filtra por cliente_id como o Supabase
faria; as consultas reais (filtros e colunas cifradas) são testadas com o
cliente falso de test_db_queries.
"""

import io
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import analyzer, db, patient_crypto
from web import config, prontuario, security

CPF_A = "529.982.247-25"
CPF_B = "111.444.777-35"


# ── Cifra e CPF ───────────────────────────────────────────────────────────────

def test_cpf_validation():
    assert patient_crypto.is_valid_cpf(CPF_A)
    assert patient_crypto.is_valid_cpf("11144477735")
    assert not patient_crypto.is_valid_cpf("529.982.247-24")
    assert not patient_crypto.is_valid_cpf("111.111.111-11")
    assert not patient_crypto.is_valid_cpf("123")


def test_encrypt_roundtrip_and_ciphertext_hides_value():
    token = patient_crypto.encrypt("Maria da Silva")
    assert "Maria" not in token
    assert patient_crypto.decrypt(token) == "Maria da Silva"
    assert patient_crypto.decrypt("lixo") == ""


def test_cpf_hash_ignores_formatting_and_mask_hides_digits():
    assert patient_crypto.cpf_hash(CPF_A) == patient_crypto.cpf_hash("52998224725")
    assert patient_crypto.cpf_hash(CPF_A) != patient_crypto.cpf_hash(CPF_B)
    assert patient_crypto.mask_cpf(CPF_A) == "***.982.247-**"


def test_production_without_key_disables_prontuario(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.delenv("PRONTUARIO_ENCRYPTION_KEY", raising=False)
    assert not patient_crypto.is_configured()
    assert db.criar_paciente("c1", "Maria", CPF_A) == (None, db.PACIENTE_INDISPONIVEL)


def test_configured_key_is_used(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = patient_crypto.encrypt("Ana")
    monkeypatch.setenv("PRONTUARIO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert patient_crypto.decrypt(token) == ""  # outra chave não abre


# ── Consultas ao banco ────────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, calls, rows):
        self._calls, self._rows = calls, rows

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return self
        return _record

    def execute(self):
        return SimpleNamespace(data=self._rows.pop(0) if self._rows else [])


class _FakeClient:
    def __init__(self, results=None):
        self.calls, self._results = [], list(results or [])

    def table(self, name):
        self.calls.append(("table", (name,), {}))
        return _FakeQuery(self.calls, self._results)


def test_criar_paciente_grava_so_texto_cifrado(monkeypatch):
    fake = _FakeClient(results=[[], [{"id": "p1"}]])  # 1) checagem de duplicado 2) insert
    monkeypatch.setattr(db, "_get_client", lambda: fake)

    paciente_id, erro = db.criar_paciente("cliente-a", "Maria da Silva", CPF_A, "1980-05-01", "Feminino")

    assert (paciente_id, erro) == ("p1", None)
    payload = next(args[0] for name, args, _ in fake.calls if name == "insert")
    assert "Maria" not in str(payload) and "52998224725" not in str(payload) and CPF_A not in str(payload)
    assert patient_crypto.decrypt(payload["nome_cifrado"]) == "Maria da Silva"
    assert patient_crypto.decrypt(payload["cpf_cifrado"]) == "52998224725"
    assert payload["cliente_id"] == "cliente-a"


def test_criar_paciente_recusa_cpf_duplicado_do_mesmo_profissional(monkeypatch):
    fake = _FakeClient(results=[[{"id": "existente"}]])
    monkeypatch.setattr(db, "_get_client", lambda: fake)

    assert db.criar_paciente("cliente-a", "Maria", CPF_A) == (None, db.PACIENTE_DUPLICADO)
    assert ("eq", ("cliente_id", "cliente-a"), {}) in fake.calls
    assert not any(name == "insert" for name, _, _ in fake.calls)


@pytest.mark.parametrize("call", [
    lambda: db.buscar_paciente("p1", "cliente-a"),
    lambda: db.listar_pacientes("cliente-a"),
    lambda: db.listar_tratamentos("p1", "cliente-a"),
    lambda: db.listar_evolucoes("p1", "cliente-a"),
    lambda: db.listar_analises_paciente("p1", "cliente-a"),
    lambda: db.vincular_analise_paciente("laudo-1", "p1", "cliente-a"),
    lambda: db.excluir_paciente("p1", "cliente-a"),
])
def test_consultas_do_prontuario_filtram_pelo_profissional(monkeypatch, call):
    fake = _FakeClient()
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    call()
    assert ("eq", ("cliente_id", "cliente-a"), {}) in fake.calls


def test_consultas_sem_profissional_nao_tocam_no_banco(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    assert db.buscar_paciente("p1", None) is None
    assert db.listar_pacientes(None) == []
    assert fake.calls == []


def test_listar_pacientes_decifra_e_ordena_por_nome(monkeypatch):
    rows = [
        {"id": "p2", "nome_cifrado": patient_crypto.encrypt("Zélia"), "cpf_cifrado": patient_crypto.encrypt("11144477735")},
        {"id": "p1", "nome_cifrado": patient_crypto.encrypt("ana"), "cpf_cifrado": patient_crypto.encrypt("52998224725")},
    ]
    monkeypatch.setattr(db, "_get_client", lambda: _FakeClient(results=[rows]))

    pacientes = db.listar_pacientes("cliente-a")

    assert [p["nome"] for p in pacientes] == ["ana", "Zélia"]
    assert pacientes[0]["cpf"] == CPF_A and pacientes[0]["cpf_mascarado"] == "***.982.247-**"
    assert "nome_cifrado" not in pacientes[0] and "cpf_cifrado" not in pacientes[0]


def test_salvar_analise_grava_paciente(monkeypatch):
    fake = _FakeClient(results=[[{"id": "laudo-1"}]])
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    db.salvar_analise("joelho", "laudo", "m", 0, 1, "premium", cliente_id="cliente-a", paciente_id="p1")
    payload = next(args[0] for name, args, _ in fake.calls if name == "insert")
    assert payload["paciente_id"] == "p1"


# ── Rotas ─────────────────────────────────────────────────────────────────────

class _FakeProntuario:
    """Pacientes de dois profissionais; toda leitura filtra por cliente_id."""

    def __init__(self):
        self.pacientes = {
            "pac-a": {"id": "pac-a", "cliente_id": "cliente-a", "nome": "Maria da Silva", "cpf": CPF_A,
                      "cpf_mascarado": "***.982.247-**", "data_nascimento": "1980-05-01", "sexo": "Feminino",
                      "observacoes": "", "created_at": "2026-10-01T10:00:00+00:00"},
            "pac-b": {"id": "pac-b", "cliente_id": "cliente-b", "nome": "João Souza", "cpf": CPF_B,
                      "cpf_mascarado": "***.444.777-**", "data_nascimento": None, "sexo": None,
                      "observacoes": "", "created_at": "2026-10-01T10:00:00+00:00"},
        }
        self.tratamentos, self.evolucoes, self.acessos, self.vinculos = [], [], [], []

    def buscar_sessao_cliente(self, email):
        cliente_id = {"a@example.com": "cliente-a", "b@example.com": "cliente-b"}.get(email)
        return {"id": cliente_id, "sessao_versao": 0} if cliente_id else None

    def buscar_paciente(self, paciente_id, cliente_id):
        p = self.pacientes.get(paciente_id)
        return dict(p) if p and p["cliente_id"] == cliente_id else None

    def listar_pacientes(self, cliente_id, limit=1000):
        return [dict(p) for p in self.pacientes.values() if p["cliente_id"] == cliente_id]

    def criar_paciente(self, cliente_id, nome, cpf, data_nascimento=None, sexo=None, observacoes=""):
        if any(p["cliente_id"] == cliente_id and p["cpf"] == cpf for p in self.pacientes.values()):
            return None, db.PACIENTE_DUPLICADO
        self.pacientes["pac-novo"] = {"id": "pac-novo", "cliente_id": cliente_id, "nome": nome, "cpf": cpf,
                                      "cpf_mascarado": "", "data_nascimento": data_nascimento, "sexo": sexo,
                                      "observacoes": observacoes, "created_at": "2026-10-22T10:00:00+00:00"}
        return "pac-novo", None

    def salvar_tratamento(self, **kwargs):
        self.tratamentos.append(kwargs)
        return "trat-1"

    def salvar_evolucao(self, **kwargs):
        self.evolucoes.append(kwargs)
        return "evo-1"

    def listar_tratamentos(self, paciente_id, cliente_id):
        return [dict(t, id="trat-1") for t in self.tratamentos if t["paciente_id"] == paciente_id]

    def listar_evolucoes(self, paciente_id, cliente_id):
        return [dict(e, id="evo-1") for e in self.evolucoes if e["paciente_id"] == paciente_id]

    def registrar_acesso_prontuario(self, cliente_id, paciente_id, acao):
        self.acessos.append((cliente_id, paciente_id, acao))

    def vincular_analise_paciente(self, analise_id, paciente_id, cliente_id):
        self.vinculos.append((analise_id, paciente_id, cliente_id))
        return True


@pytest.fixture()
def fake_prontuario(monkeypatch):
    fake = _FakeProntuario()
    for name in (
        "buscar_sessao_cliente", "buscar_paciente", "listar_pacientes", "criar_paciente", "salvar_tratamento",
        "salvar_evolucao", "listar_tratamentos", "listar_evolucoes", "registrar_acesso_prontuario",
        "vincular_analise_paciente",
    ):
        monkeypatch.setattr(db, name, getattr(fake, name))
    monkeypatch.setattr(db, "listar_analises_paciente", lambda paciente_id, cliente_id: [])
    return fake


def _login_as(client, email):
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", email, 3600))


def test_lista_mostra_so_os_pacientes_do_profissional(client, fake_prontuario):
    _login_as(client, "a@example.com")
    html = client.get("/pacientes").get_data(as_text=True)
    assert "Maria da Silva" in html and "João Souza" not in html
    assert CPF_A not in html  # na lista o CPF aparece mascarado


def test_busca_por_nome_e_cpf(client, fake_prontuario):
    _login_as(client, "a@example.com")
    assert "Maria da Silva" in client.get("/pacientes?q=maria").get_data(as_text=True)
    assert "Maria da Silva" in client.get("/pacientes?q=52998").get_data(as_text=True)
    assert "Maria da Silva" not in client.get("/pacientes?q=pedro").get_data(as_text=True)


def test_prontuario_requer_login(client):
    assert client.get("/pacientes").status_code == 302


def test_admin_nao_ve_prontuarios(client, fake_prontuario):
    client.get(f"/admin/entrar?key={config.get_admin_key()}")
    assert "contas de profissional" in client.get("/pacientes").get_data(as_text=True)
    assert client.get("/pacientes/pac-a").status_code == 404


@pytest.mark.parametrize("method,path", [
    ("get", "/pacientes/pac-b"),
    ("get", "/pacientes/pac-b/editar"),
    ("get", "/pacientes/pac-b/contexto"),
    ("post", "/pacientes/pac-b/tratamentos"),
    ("post", "/pacientes/pac-b/evolucoes"),
    ("post", "/pacientes/pac-b/excluir"),
])
def test_profissional_nao_acessa_paciente_de_outro(client, fake_prontuario, method, path):
    _login_as(client, "a@example.com")
    resp = getattr(client, method)(path, data={"tipo": "Fisioterapia", "data_inicio": "2026-10-01", "data": "2026-10-01",
                                               "dor_eva": "5", "confirmacao": "EXCLUIR"})
    assert resp.status_code == 404
    assert fake_prontuario.tratamentos == [] and fake_prontuario.evolucoes == []


def test_detalhe_registra_acesso_na_auditoria(client, fake_prontuario):
    _login_as(client, "a@example.com")
    html = client.get("/pacientes/pac-a").get_data(as_text=True)
    assert "Maria da Silva" in html and CPF_A in html
    assert ("cliente-a", "pac-a", "visualizar") in fake_prontuario.acessos


def test_cadastro_valido(client, fake_prontuario):
    _login_as(client, "a@example.com")
    resp = client.post("/pacientes/novo", data={
        "nome": "  Ana   Lima ", "cpf": "111.444.777-35", "data_nascimento": "1990-02-03",
        "sexo": "Feminino", "consentimento": "accepted",
    })
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pacientes/pac-novo")
    novo = fake_prontuario.pacientes["pac-novo"]
    assert novo["cliente_id"] == "cliente-a" and novo["nome"] == "Ana Lima" and novo["cpf"] == CPF_B


@pytest.mark.parametrize("form,mensagem", [
    ({"nome": "Ana Lima", "cpf": "123.456.789-00", "consentimento": "accepted"}, "CPF inválido"),
    ({"nome": "Ana Lima", "cpf": CPF_B}, "autorizou o registro"),
    ({"nome": "A", "cpf": CPF_B, "consentimento": "accepted"}, "nome completo"),
    ({"nome": "Ana Lima", "cpf": CPF_B, "consentimento": "accepted", "data_nascimento": "2999-01-01"}, "nascimento inválida"),
])
def test_cadastro_invalido(client, fake_prontuario, form, mensagem):
    _login_as(client, "a@example.com")
    resp = client.post("/pacientes/novo", data=form)
    assert resp.status_code == 400 and mensagem in resp.get_data(as_text=True)
    assert "pac-novo" not in fake_prontuario.pacientes


def test_cadastro_duplicado(client, fake_prontuario):
    _login_as(client, "a@example.com")
    resp = client.post("/pacientes/novo", data={"nome": "Maria", "cpf": CPF_A, "consentimento": "accepted"})
    assert resp.status_code == 409 and "Já existe um paciente com este CPF" in resp.get_data(as_text=True)


def test_registra_tratamento_e_evolucao(client, fake_prontuario):
    _login_as(client, "a@example.com")
    client.post("/pacientes/pac-a/tratamentos", data={
        "tipo": "Fisioterapia", "data_inicio": "2026-09-01", "regiao": "joelho direito",
    })
    client.post("/pacientes/pac-a/evolucoes", data={"data": "2026-09-15", "dor_eva": "6", "funcao": "55"})
    client.post("/pacientes/pac-a/evolucoes", data={"data": "2026-10-15", "dor_eva": "3", "funcao": "80"})

    assert fake_prontuario.tratamentos[0]["cliente_id"] == "cliente-a"
    assert [e["dor_eva"] for e in fake_prontuario.evolucoes] == [6, 3]
    html = client.get("/pacientes/pac-a").get_data(as_text=True)
    assert "<svg" in html and "polyline" in html and "Início do tratamento: Fisioterapia" in html


@pytest.mark.parametrize("form", [
    {"data": "2026-10-01", "dor_eva": "11"},
    {"data": "2026-10-01", "funcao": "abc"},
    {"data": "2026-10-01"},
    {"dor_eva": "3"},
])
def test_evolucao_invalida_nao_e_salva(client, fake_prontuario, form):
    _login_as(client, "a@example.com")
    client.post("/pacientes/pac-a/evolucoes", data=form)
    assert fake_prontuario.evolucoes == []


def test_contexto_da_analise_nao_expoe_identificacao(client, fake_prontuario):
    fake_prontuario.tratamentos.append({"paciente_id": "pac-a", "tipo": "Infiltração", "data_inicio": "2026-08-01",
                                        "data_fim": None, "regiao": "ombro", "descricao": ""})
    _login_as(client, "a@example.com")
    data = client.get("/pacientes/pac-a/contexto").get_json()
    assert data["sexo"] == "Feminino" and isinstance(data["idade"], int)
    assert data["tratamentos"] == "Infiltração (ombro, desde 08/2026)"
    assert "Maria" not in str(data) and "982" not in str(data)


def test_index_lista_pacientes_do_profissional(client, fake_prontuario):
    _login_as(client, "a@example.com")
    html = client.get("/?paciente=pac-a").get_data(as_text=True)
    assert 'name="paciente_id"' in html and "Maria da Silva" in html and "João Souza" not in html
    assert '<option value="pac-a" data-idade=' in html and "selected" in html


FAKE_RESULT = {
    "success": True, "exam_type": "joelho", "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\nlaudo",
    "references_used": 0, "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [], "cache_hit": False,
}


def _analyze(client, paciente_id):
    return client.post("/analyze", data={
        "exam_image": (io.BytesIO(b"fake-image-bytes"), "joelho.jpg"),
        "privacy_consent": "accepted",
        "paciente_id": paciente_id,
        "description": "Dor no joelho há 3 meses",
    }, content_type="multipart/form-data")


def test_analise_vinculada_ao_paciente(client, fake_prontuario, monkeypatch):
    analyze = MagicMock(return_value=dict(FAKE_RESULT))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    salvar = MagicMock(return_value="laudo-1")
    monkeypatch.setattr(db, "salvar_analise_com_imagens", salvar)
    _login_as(client, "a@example.com")

    resp = _analyze(client, "pac-a")

    assert resp.status_code == 200 and "/pacientes/pac-a" in resp.get_data(as_text=True)
    assert salvar.call_args.kwargs["paciente_id"] == "pac-a"
    sent = analyze.call_args.kwargs["user_description"]
    assert "Maria" not in sent and "529" not in sent  # identificação nunca vai para a IA


def test_analise_recusa_paciente_de_outro_profissional(client, fake_prontuario, monkeypatch):
    analyze = MagicMock(return_value=dict(FAKE_RESULT))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    _login_as(client, "a@example.com")

    resp = _analyze(client, "pac-b")

    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    analyze.assert_not_called()


def test_vincular_laudo_existente(client, fake_prontuario):
    _login_as(client, "a@example.com")
    resp = client.post("/laudos/laudo-1/paciente", data={"paciente_id": "pac-a"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pacientes/pac-a")
    assert fake_prontuario.vinculos == [("laudo-1", "pac-a", "cliente-a")]

    resp = client.post("/laudos/laudo-1/paciente", data={"paciente_id": "pac-b"})
    assert len(fake_prontuario.vinculos) == 1  # paciente de outro profissional: nada muda


def test_sem_conta_profissional_nao_cadastra_paciente(client, fake_prontuario):
    client.get(f"/admin/entrar?key={config.get_admin_key()}")
    resp = client.post("/pacientes/novo", data={"nome": "Ana Lima", "cpf": CPF_B, "consentimento": "accepted"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pacientes")
    assert "pac-novo" not in fake_prontuario.pacientes


def test_sem_plano_nao_acessa_prontuario(client, fake_prontuario):
    resp = client.post("/pacientes/novo", data={"nome": "Ana Lima", "cpf": CPF_B, "consentimento": "accepted"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    assert "pac-novo" not in fake_prontuario.pacientes


# ── Exclusão da conta x cópia dos prontuários ─────────────────────────────────

def _conta_com_pacientes(client, monkeypatch, copia_em_dia, exportado_em=None):
    from werkzeug.security import generate_password_hash

    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda e: generate_password_hash("senha"))
    monkeypatch.setattr(db, "resumo_backup_prontuarios", lambda cid: {
        "pacientes": 3, "exportado_em": exportado_em, "copia_em_dia": copia_em_dia,
    })
    excluir = MagicMock(return_value=True)
    monkeypatch.setattr(db, "excluir_dados_cliente", excluir)
    _login_as(client, "a@example.com")
    return excluir


def test_exclusao_sem_copia_exige_ciencia_dos_prontuarios(client, monkeypatch):
    excluir = _conta_com_pacientes(client, monkeypatch, copia_em_dia=False)

    html = client.get("/conta").get_data(as_text=True)
    assert "Você ainda não baixou uma cópia" in html and "3 pacientes" in html

    resp = client.post("/conta/excluir", data={"senha": "senha", "confirmacao": "EXCLUIR"})
    assert resp.headers["Location"].endswith("/conta")
    excluir.assert_not_called()

    client.post("/conta/excluir", data={"senha": "senha", "confirmacao": "EXCLUIR", "ciente_prontuarios": "accepted"})
    excluir.assert_called_once_with("cliente-a")


def test_exclusao_com_copia_em_dia_apaga_sem_aviso_extra(client, monkeypatch):
    excluir = _conta_com_pacientes(client, monkeypatch, copia_em_dia=True, exportado_em="2026-10-20T10:00:00+00:00")
    assert "ciente_prontuarios" not in client.get("/conta").get_data(as_text=True)
    client.post("/conta/excluir", data={"senha": "senha", "confirmacao": "EXCLUIR"})
    excluir.assert_called_once_with("cliente-a")


def test_aviso_quando_prontuario_mudou_depois_da_copia(client, monkeypatch):
    _conta_com_pacientes(client, monkeypatch, copia_em_dia=False, exportado_em="2026-10-01T10:00:00+00:00")
    assert "mudaram depois da última cópia" in client.get("/conta").get_data(as_text=True)


@pytest.mark.parametrize("exportado_em,alteracoes,pacientes,em_dia", [
    (None, [], 0, True),                                          # sem pacientes: nada a perder
    (None, ["2026-10-01T00:00:00+00:00"], 2, False),              # nunca baixou
    ("2026-10-05T00:00:00+00:00", ["2026-10-01T00:00:00+00:00"], 2, True),
    ("2026-10-05T00:00:00+00:00", ["2026-10-09T00:00:00+00:00"], 2, False),  # mudou depois
])
def test_resumo_backup_prontuarios(monkeypatch, exportado_em, alteracoes, pacientes, em_dia):
    results = [
        [{"id": f"p{i}"} for i in range(pacientes)],
        [{"dados_exportados_em": exportado_em}],
        [{"updated_at": a} for a in alteracoes],
        [],
        [],
    ]
    monkeypatch.setattr(db, "_get_client", lambda: _FakeClient(results=results))
    assert db.resumo_backup_prontuarios("cliente-a")["copia_em_dia"] is em_dia


# ── Linha do tempo e gráfico ──────────────────────────────────────────────────

def test_idade():
    assert prontuario.idade("1980-05-01", hoje=date(2026, 4, 30)) == 45
    assert prontuario.idade("1980-05-01", hoje=date(2026, 5, 1)) == 46
    assert prontuario.idade(None) is None


def test_linha_do_tempo_junta_eventos_do_mais_recente_ao_mais_antigo():
    eventos = prontuario.linha_do_tempo(
        analises=[{"id": "l1", "created_at": "2026-08-20T12:00:00+00:00", "tipo_exame": "joelho"}],
        tratamentos=[{"tipo": "Fisioterapia", "data_inicio": "2026-09-01", "data_fim": "2026-10-01"}],
        evolucoes=[{"data": "2026-10-10", "dor_eva": 2, "funcao": 90, "nota": "Melhora"}],
    )
    assert [e["tipo"] for e in eventos] == ["evolucao", "tratamento_fim", "tratamento", "laudo"]
    assert eventos[0]["titulo"] == "Evolução: Dor (EVA) 2/10 · Função 90/100"
    assert eventos[-1]["analise_id"] == "l1"


def test_grafico_escala_dor_e_funcao():
    grafico = prontuario.grafico_evolucao(
        evolucoes=[{"data": "2026-09-01", "dor_eva": 10, "funcao": 0}, {"data": "2026-10-01", "dor_eva": 0, "funcao": 100}],
        tratamentos=[{"tipo": "Fisioterapia", "data_inicio": "2026-09-01"}],
        analises=[],
    )
    primeiro, ultimo = grafico["dor"]
    assert primeiro["x"] == grafico["left"] and ultimo["x"] == grafico["right"]
    assert primeiro["y"] == grafico["top"] and ultimo["y"] == grafico["bottom"]  # dor 10 no topo, 0 embaixo
    assert grafico["funcao"][1]["y"] == grafico["top"]
    assert grafico["tratamentos"][0]["x"] == grafico["left"]
    assert prontuario.grafico_evolucao([{"data": "2026-09-01", "nota": "só nota"}], [], []) is None
