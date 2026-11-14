"""
Registro profissional conferido, termo de responsabilidade, "a IA errou isso",
leitura pela CIF e ponte do laudo para o plano de tratamento.
"""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import analyzer, db
from core import professional_register as registro
from evals import import_feedback
from web import presentation, professional, prontuario, security

FAKE_RESULT = {
    "success": True, "exam_type": "joelho", "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\nlaudo",
    "references_used": 0, "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [], "cache_hit": False,
}

LAUDO_COM_PLANO = """## 0. SINAIS DE ALERTA E ENCAMINHAMENTO
**Nenhum sinal de alerta identificado nas imagens analisadas.**
## 6. CONDUTA, ENCAMINHAMENTO E REABILITAÇÃO
- **Considerações para a reabilitação:** proteger o enxerto.

**Plano de reabilitação — metas e reavaliação:**
- Meta de curto prazo (2–4 semanas): **subir escadas** sem dor > 3/10
- Meta de médio prazo (6–12 semanas): retorno à corrida leve
- Medidas para a avaliação inicial e a reavaliação: END, KOOS, goniometria
- Reavaliar em: 4 semanas, ou antes se houver derrame

## 7. REFERÊNCIAS CIENTÍFICAS (PubMed)
- Plano de reabilitação citado num título de artigo
"""


# ── Registro profissional ─────────────────────────────────────────────────────

@pytest.mark.parametrize("perfil, entrada, esperado", [
    ("Médico", "crm/sp 123.456", "CRM-SP 123456"),
    ("Ortopedista", "123456-RJ", "CRM-RJ 123456"),
    ("Fisioterapeuta", "crefito 3/123456-F", "CREFITO-3 123456-F"),
    ("Fisioterapeuta", "Crefito-10 12345", "CREFITO-10 12345-F"),
    ("Outro profissional de saúde", "coren-sp 123456", "COREN-SP 123456"),
    ("", "CRM SP 98765", "CRM-SP 98765"),
    ("", "", ""),
    ("Estudante da área da saúde", "matrícula 2024", ""),
])
def test_registro_valido_e_normalizado(perfil, entrada, esperado):
    assert registro.validar_registro(perfil, entrada) == (esperado, None)


@pytest.mark.parametrize("perfil, entrada, trecho", [
    ("Médico", "", "Informe o registro"),
    ("Fisioterapeuta", "", "CREFITO-3 123456-F"),
    ("Médico", "CRM-XX 1234", "UF do conselho"),
    ("Médico", "CREFITO-3 1234-F", "usa registro no CRM"),
    ("Fisioterapeuta", "CRM-SP 1234", "usa registro no CREFITO"),
    ("Fisioterapeuta", "CREFITO-3 1234-TO", "terapia ocupacional"),
    ("Fisioterapeuta", "CREFITO-40 1234-F", "fora do formato"),
    ("Outro profissional de saúde", "banana", "fora do formato"),
])
def test_registro_invalido_explica_o_problema(perfil, entrada, trecho):
    normalizado, erro = registro.validar_registro(perfil, entrada)
    assert normalizado == "" and trecho in erro


# ── Conta de profissional (fakes) ─────────────────────────────────────────────

@pytest.fixture()
def conta(monkeypatch):
    """Conta "cliente-a" com perfil em memória; o termo começa sem aceite."""
    estado = SimpleNamespace(perfil={}, aceites=[], salvos=[], erros=[], analises={})
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_perfil_profissional", lambda cliente_id: dict(estado.perfil))

    def aceitar(cliente_id, versao):
        estado.aceites.append((cliente_id, versao))
        estado.perfil["termo_versao"] = versao
        return True

    def salvar_perfil(cliente_id, campos):
        estado.salvos.append(campos)
        estado.perfil.update({k: v for k, v in campos.items() if v})
        return True

    def salvar_erro(**kwargs):
        estado.erros.append(kwargs)
        return "fb-1"

    monkeypatch.setattr(db, "registrar_aceite_termo", aceitar)
    monkeypatch.setattr(db, "salvar_perfil_profissional", salvar_perfil)
    monkeypatch.setattr(db, "salvar_erro_ia", salvar_erro)
    monkeypatch.setattr(db, "buscar_analise", lambda analise_id, cliente_id=None, include_all=False:
                        estado.analises.get(analise_id) if cliente_id == "cliente-a" else None)
    monkeypatch.setattr(db, "buscar_marca_clinica", lambda cliente_id: {})
    monkeypatch.setattr(db, "resumo_backup_prontuarios", lambda cliente_id: None)
    return estado


def _login(client):
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))


def _analyze(client, **extra):
    data = {"exam_image": (io.BytesIO(b"fake-image-bytes"), "joelho.jpg"), "privacy_consent": "accepted"}
    data.update(extra)
    return client.post("/analyze", data=data, content_type="multipart/form-data")


@pytest.fixture()
def analisar(monkeypatch):
    mock = MagicMock(return_value=dict(FAKE_RESULT))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", mock)
    monkeypatch.setattr(db, "salvar_analise_com_imagens", MagicMock(return_value="laudo-1"))
    return mock


# ── Termo de responsabilidade ─────────────────────────────────────────────────

def test_analise_sem_aceite_do_termo_e_recusada(client, conta, analisar):
    _login(client)
    resp = _analyze(client)
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    analisar.assert_not_called()
    assert conta.aceites == []


def test_aceite_no_formulario_e_gravado_com_a_versao(client, conta, analisar):
    _login(client)
    resp = _analyze(client, termo_responsabilidade="accepted")
    assert resp.status_code == 200
    assert conta.aceites == [("cliente-a", professional.TERMO_VERSAO)]
    analisar.assert_called_once()


def test_termo_ja_aceito_nao_e_pedido_de_novo(client, conta, analisar):
    conta.perfil["termo_versao"] = professional.TERMO_VERSAO
    _login(client)
    assert _analyze(client).status_code == 200
    assert conta.aceites == []
    assert 'name="termo_responsabilidade"' not in client.get("/").get_data(as_text=True)


def test_versao_nova_do_termo_pede_aceite_de_novo(client, conta, analisar):
    conta.perfil["termo_versao"] = "2020-01"
    _login(client)
    assert 'name="termo_responsabilidade"' in client.get("/").get_data(as_text=True)
    assert _analyze(client).status_code == 302


def test_pagina_do_termo_e_publica_e_cita_os_conselhos(client):
    html = client.get("/termo-responsabilidade").get_data(as_text=True)
    assert "Resolução COFFITO nº 424/2013" in html and "Lei 12.842/2013" in html
    assert professional.TERMO_VERSAO in html


# ── Registro no envio do exame ────────────────────────────────────────────────

def test_fisioterapeuta_sem_crefito_valido_nao_envia(client, conta, analisar):
    conta.perfil["termo_versao"] = professional.TERMO_VERSAO
    _login(client)
    resp = _analyze(client, responsible_role="Fisioterapeuta", responsible_register="CRM-SP 1234")
    assert resp.status_code == 302
    analisar.assert_not_called()


def test_registro_vai_normalizado_para_o_laudo(client, conta, analisar):
    conta.perfil["termo_versao"] = professional.TERMO_VERSAO
    _login(client)
    resp = _analyze(client, responsible_role="Fisioterapeuta", responsible_register="crefito 3/123456")
    assert resp.status_code == 200
    assert "CREFITO-3 123456-F" in resp.get_data(as_text=True)
    persist = db.salvar_analise_com_imagens.call_args.kwargs
    assert persist["responsavel"]["register"] == "CREFITO-3 123456-F"


def test_formulario_vem_preenchido_com_o_perfil_da_conta(client, conta):
    conta.perfil.update({"profissional_nome": "Dra. Ana", "profissional_perfil": "Fisioterapeuta",
                         "profissional_registro": "CREFITO-3 123456-F", "termo_versao": professional.TERMO_VERSAO})
    _login(client)
    html = client.get("/").get_data(as_text=True)
    assert 'value="Dra. Ana"' in html and 'value="CREFITO-3 123456-F"' in html
    assert '<option value="Fisioterapeuta" selected>' in html
    assert "Estudante da área da saúde" in html


def test_salvar_perfil_normaliza_e_registra_aceite(client, conta):
    _login(client)
    resp = client.post("/conta/perfil", data={
        "profissional_nome": "Dra. Ana", "profissional_perfil": "Médico",
        "profissional_registro": "crm sp 123456", "termo_responsabilidade": "accepted",
    })
    assert resp.status_code == 302
    assert conta.salvos[-1]["profissional_registro"] == "CRM-SP 123456"
    assert conta.salvos[-1]["termo_versao"] == professional.TERMO_VERSAO


def test_salvar_perfil_com_registro_invalido_nao_grava(client, conta):
    _login(client)
    client.post("/conta/perfil", data={"profissional_perfil": "Fisioterapeuta", "profissional_registro": "123"})
    client.post("/conta/perfil", data={"profissional_perfil": "Curandeiro", "profissional_registro": ""})
    assert conta.salvos == []


# ── "A IA errou isso" ─────────────────────────────────────────────────────────

def test_erro_apontado_em_um_clique(client, conta):
    conta.analises["laudo-1"] = {"id": "laudo-1", "analise_completa": "laudo"}
    _login(client)
    resp = client.post("/feedback/erro", json={
        "analysis_id": "laudo-1", "category": "alerta_perdido", "comment": "fratura do escafoide", "allow_eval": True,
    })
    assert resp.status_code == 200 and resp.get_json()["success"]
    [erro] = conta.erros
    assert erro["categoria"] == "alerta_perdido" and erro["tipo"] == "falso_negativo"
    assert erro["autoriza_avaliacao"] is True and erro["comentario"] == "fratura do escafoide"


@pytest.mark.parametrize("payload, status", [
    ({"analysis_id": "laudo-1", "category": "nao_existe"}, 400),
    ({"analysis_id": "", "category": "achado_perdido"}, 400),
    ({"analysis_id": "laudo-de-outro", "category": "achado_perdido"}, 404),
])
def test_erro_apontado_invalido_ou_de_outro_profissional(client, conta, payload, status):
    conta.analises["laudo-1"] = {"id": "laudo-1"}
    _login(client)
    assert client.post("/feedback/erro", json=payload).status_code == status
    assert conta.erros == []


def test_botoes_de_erro_aparecem_no_laudo_salvo(client, conta, monkeypatch):
    conta.analises["laudo-1"] = {"id": "laudo-1", "analise_completa": "## 1. IDENTIFICAÇÃO DO EXAME\nx",
                                 "tipo_exame": "joelho", "created_at": "2026-11-14T10:00:00+00:00"}
    monkeypatch.setattr(db, "buscar_dados_estruturados", lambda *a, **k: None, raising=False)
    _login(client)
    html = client.get("/laudos/laudo-1").get_data(as_text=True)
    for cat in presentation.ERROR_CATEGORIES.values():
        assert cat["label"] in html
    # Consentimento para uso nos testes de qualidade nunca vem marcado (LGPD: aceite afirmativo).
    assert '<input type="checkbox" id="ai-error-allow">' in html


class _FakeTable:
    def __init__(self, calls, fail_first):
        self.calls, self.fail_first = calls, fail_first

    def insert(self, row):
        self.calls.append(row)
        return self

    def execute(self):
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError('column "categoria_erro" does not exist')
        return SimpleNamespace(data=[{"id": f"fb-{len(self.calls)}"}])


@pytest.mark.parametrize("fail_first", [False, True])
def test_salvar_erro_ia_funciona_antes_e_depois_da_migration(monkeypatch, fail_first):
    calls = []
    monkeypatch.setattr(db, "_get_client", lambda: SimpleNamespace(table=lambda name: _FakeTable(calls, fail_first)))
    assert db.salvar_erro_ia("laudo-1", "achado_inventado", "falso_positivo", "menisco íntegro", True)
    if not fail_first:
        assert calls[0]["secao"] == "erro_ia" and calls[0]["categoria_erro"] == "achado_inventado"
    else:
        assert calls[1]["secao"] == "validacao_medica"
        assert calls[1]["comentario"] == "[erro_ia:achado_inventado] menisco íntegro"
        assert "autoriza_avaliacao" not in calls[1]


# ── Feedback → evals ──────────────────────────────────────────────────────────

def test_parse_feedback_entende_os_dois_formatos():
    novo = {"secao": "erro_ia", "categoria_erro": "alerta_perdido", "comentario": "x", "autoriza_avaliacao": True}
    antigo = {"secao": "validacao_medica", "comentario": "[erro_ia:regiao_lado] lado direito"}
    assert import_feedback.parse_feedback(novo) == {"categoria": "alerta_perdido", "comentario": "x", "autoriza": True}
    assert import_feedback.parse_feedback(antigo) == {"categoria": "regiao_lado", "comentario": "lado direito",
                                                      "autoriza": False}
    assert import_feedback.parse_feedback({"secao": "validacao_medica", "comentario": "Concordo"}) is None


def test_rascunho_tem_formato_de_caso_e_preserva_o_que_ja_foi_preenchido():
    analise = {"id": "abcdef12-0000", "tipo_exame": "mao_punho", "num_imagens": 2, "descricao_usuario": "queda",
               "analise_completa": "laudo", "created_at": "2026-11-10T00:00:00Z"}
    draft = import_feedback.build_draft(analise, [{"categoria": "alerta_perdido", "comentario": "escafoide"}])
    assert draft["images"] == ["images/abcdef12-1.jpg", "images/abcdef12-2.jpg"]
    assert draft["expected_region"] == "mao_punho"
    assert any("expected_red_flags" in hint for hint in draft["rascunho"]["o_que_preencher"])

    draft["expected_findings"] = [["fratura do escafoide"]]
    again = import_feedback.build_draft(analise, [{"categoria": "achado_perdido", "comentario": "edema"}],
                                        json.loads(json.dumps(draft)))
    assert again["expected_findings"] == [["fratura do escafoide"]]
    assert [a["categoria"] for a in again["rascunho"]["apontamentos"]] == ["alerta_perdido", "achado_perdido"]


# ── CIF e plano de tratamento ─────────────────────────────────────────────────

def test_prompt_pede_leitura_pela_cif_e_plano_com_rotulo_fixo():
    prompt = analyzer.build_analysis_prompt("joelho")
    assert "5e. Leitura pela CIF" in prompt and "Atividades e participação (d)" in prompt
    assert "**Plano de reabilitação — metas e reavaliação:**" in prompt
    assert "na dúvida, escreva só o nome da categoria" in prompt


def test_sintese_preserva_cif_e_plano():
    import inspect

    source = inspect.getsource(analyzer._synthesize_analyses)
    assert "5e. Leitura pela CIF" in source and "Plano de reabilitação — metas e reavaliação:" in source


def test_plano_do_laudo_extrai_so_os_itens_do_bloco():
    plano = prontuario.plano_do_laudo(LAUDO_COM_PLANO)
    assert plano.splitlines() == [
        "- Meta de curto prazo (2–4 semanas): subir escadas sem dor > 3/10",
        "- Meta de médio prazo (6–12 semanas): retorno à corrida leve",
        "- Medidas para a avaliação inicial e a reavaliação: END, KOOS, goniometria",
        "- Reavaliar em: 4 semanas, ou antes se houver derrame",
    ]
    assert prontuario.plano_do_laudo("## 6. CONDUTA E PRÓXIMOS PASSOS\nsem bloco") is None


def test_plano_sugerido_preenche_tratamento():
    plano = prontuario.plano_sugerido({"analise_completa": LAUDO_COM_PLANO, "tipo_exame": "joelho",
                                       "created_at": "2026-11-14T10:00:00Z"})
    assert plano["tipo"] == "Fisioterapia" and plano["regiao"] == "Joelho"
    assert plano["descricao"].startswith("Plano sugerido pela análise de 14/11/2026")
    assert len(plano["descricao"]) <= 2000


def _paciente(monkeypatch, analises):
    paciente = {"id": "pac-a", "cliente_id": "cliente-a", "nome": "Maria", "cpf_mascarado": "", "data_nascimento": None,
                "sexo": None, "observacoes": "", "created_at": "2026-10-01T10:00:00+00:00"}
    monkeypatch.setattr(db, "buscar_paciente", lambda pid, cid: dict(paciente) if cid == "cliente-a" else None)
    monkeypatch.setattr(db, "listar_analises_paciente", lambda pid, cid: analises)
    monkeypatch.setattr(db, "listar_tratamentos", lambda pid, cid: [])
    monkeypatch.setattr(db, "listar_evolucoes", lambda pid, cid: [])
    monkeypatch.setattr(db, "registrar_acesso_prontuario", lambda *a: None)


def test_prontuario_preenche_formulario_com_o_plano_do_laudo(client, conta, monkeypatch):
    conta.analises["laudo-1"] = {"id": "laudo-1", "analise_completa": LAUDO_COM_PLANO, "tipo_exame": "joelho",
                                 "created_at": "2026-11-14T10:00:00Z"}
    _paciente(monkeypatch, [{"id": "laudo-1", "tipo_exame": "joelho", "created_at": "2026-11-14T10:00:00Z"}])
    _login(client)
    html = client.get("/pacientes/pac-a?plano_de=laudo-1").get_data(as_text=True)
    assert "Meta de curto prazo (2–4 semanas): subir escadas" in html
    assert '<option value="Fisioterapia" selected>' in html
    assert "Plano do laudo de 14/11/2026 copiado" in html


def test_prontuario_nao_usa_laudo_de_outro_paciente(client, conta, monkeypatch):
    conta.analises["laudo-x"] = {"id": "laudo-x", "analise_completa": LAUDO_COM_PLANO, "tipo_exame": "joelho"}
    _paciente(monkeypatch, [])
    _login(client)
    html = client.get("/pacientes/pac-a?plano_de=laudo-x").get_data(as_text=True)
    assert "subir escadas" not in html


# ── Avisos ────────────────────────────────────────────────────────────────────

def test_avisos_citam_o_codigo_de_etica_da_fisioterapia():
    from pathlib import Path

    root = Path(__file__).parent.parent
    for rel in ("templates/result.html", "templates/laudo_detalhe.html", "templates/index.html",
                "templates/trial.html", "static/laudo-pdf.js"):
        assert "Resolução COFFITO nº 424/2013" in (root / rel).read_text(encoding="utf-8"), rel
