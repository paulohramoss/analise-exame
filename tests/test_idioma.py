"""Laudo em inglês: a análise é a mesma e o resultado pronto é traduzido (core/idioma.py)."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import analyzer, comparacao, db, idioma, lab_tests, patient_report
from web import presentation, security

ROOT = Path(__file__).resolve().parents[1]

LAUDO_PT = """## 1. IDENTIFICAÇÃO DO EXAME
Ressonância do joelho direito.

## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
- **[FORTE]** Rotura do ligamento cruzado anterior.
- **[MODERADO]** Edema ósseo no côndilo femoral lateral, 14,6 mm.

## 4. IMPRESSÃO DE IMAGEM — HIPÓTESES
Lesão do LCA."""

LAUDO_EN = """## 1. EXAM IDENTIFICATION
MRI of the right knee.

## 3. FINDINGS — CLASSIFIED BY STRENGTH OF EVIDENCE
- **[STRONG]** Anterior cruciate ligament tear.
- **[MODERATE]** Bone edema in the lateral femoral condyle, 14.6 mm.

## 4. IMAGING IMPRESSION — HYPOTHESES
ACL injury."""


def _resultado(**extra):
    return {"analysis": LAUDO_PT, "usage": [{"model": "m", "input_tokens": 1, "output_tokens": 1}],
            "annotations": [{"numero": 1, "imagem": 1, "estrutura": "Ligamento cruzado anterior",
                             "local": "terço médio", "certeza": "forte", "box_2d": [1, 2, 3, 4]}],
            **extra}


def _gerador(*respostas):
    chamadas = []
    fila = list(respostas)

    def gerar(prompt, json_mode):
        chamadas.append({"prompt": prompt, "json_mode": json_mode})
        resposta = fila.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta, {"model": "tradutor", "input_tokens": 100, "output_tokens": 90}

    return gerar, chamadas


# ── Módulo de idioma ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor,esperado", [
    ("en", "en"), ("EN", "en"), ("en-US", "en"), ("english", "en"),
    ("pt-BR", "pt-BR"), ("", "pt-BR"), (None, "pt-BR"), ("fr", "pt-BR"),
])
def test_normalizar(valor, esperado):
    assert idioma.normalizar(valor) == esperado


def test_contar_etiquetas_nos_dois_idiomas():
    assert idioma.contar_etiquetas(LAUDO_PT) == (1, 1)
    assert idioma.contar_etiquetas(LAUDO_EN) == (1, 1)
    assert idioma.contar_etiquetas(None) == (0, 0)


def test_instrucao_de_escrita_so_em_ingles():
    assert idioma.instrucao_escrita("pt-BR") == ""
    assert "inglês clínico" in idioma.instrucao_escrita("en")


def test_traducao_do_laudo_e_das_marcacoes():
    gerar, chamadas = _gerador(LAUDO_EN, json.dumps({"marcacoes": [
        {"estrutura": "Anterior cruciate ligament", "local": "mid third"}]}))
    result = idioma.traduzir_resultado(_resultado(), gerar)

    assert result["analysis"] == LAUDO_EN and result["idioma"] == "en" and result["traducao_falhou"] is False
    assert not chamadas[0]["json_mode"] and LAUDO_PT in chamadas[0]["prompt"]
    assert chamadas[1]["json_mode"] and "Ligamento cruzado anterior" in chamadas[1]["prompt"]
    marca = result["annotations"][0]
    assert marca["estrutura"] == "Anterior cruciate ligament" and marca["local"] == "mid third"
    assert marca["box_2d"] == [1, 2, 3, 4] and marca["certeza"] == "forte"  # posição e nível não mudam
    assert len(result["usage"]) == 3  # análise + as duas traduções entram no custo


def test_etiqueta_esquecida_em_portugues_e_convertida():
    gerar, _ = _gerador(LAUDO_EN.replace("[STRONG]", "[FORTE]"), "{}")
    result = idioma.traduzir_resultado(_resultado(), gerar)
    assert result["idioma"] == "en" and "[FORTE]" not in result["analysis"] and "[STRONG]" in result["analysis"]


@pytest.mark.parametrize("resposta", [
    RuntimeError("provedor fora"),
    LAUDO_EN.split("## 4.")[0],  # cortou uma seção
    "",
    "## 1. ok",  # curto demais
])
def test_falha_na_traducao_mantem_o_laudo_em_portugues(resposta):
    gerar, _ = _gerador(resposta)
    result = idioma.traduzir_resultado(_resultado(), gerar)
    assert result["analysis"] == LAUDO_PT
    assert result["idioma"] == "pt-BR" and result["traducao_falhou"] is True
    assert result["annotations"][0]["estrutura"] == "Ligamento cruzado anterior"


def test_marcacoes_com_quantidade_diferente_ficam_como_estavam():
    gerar, _ = _gerador(LAUDO_EN, json.dumps({"marcacoes": []}))
    result = idioma.traduzir_resultado(_resultado(), gerar)
    assert result["idioma"] == "en" and result["annotations"][0]["estrutura"] == "Ligamento cruzado anterior"


def test_falha_so_nos_itens_mantem_o_laudo_traduzido():
    gerar, _ = _gerador(LAUDO_EN, RuntimeError("timeout"))
    result = idioma.traduzir_resultado(_resultado(), gerar)
    assert result["analysis"] == LAUDO_EN and result["idioma"] == "en"


def test_padroes_laboratoriais_sao_traduzidos():
    estruturado = {"padroes": [{"titulo": "Ferritina elevada", "explicacao": "x", "sugestao": "y", "nivel": "info",
                                "exames": ["Ferritina"]}]}
    gerar, _ = _gerador(LAUDO_EN, json.dumps({"padroes": [
        {"titulo": "High ferritin", "explicacao": "Slightly above the limit.", "sugestao": "Repeat."}]}))
    result = idioma.traduzir_resultado(_resultado(annotations=[], structured=estruturado), gerar)
    padrao = result["structured"]["padroes"][0]
    assert padrao["titulo"] == "High ferritin" and padrao["nivel"] == "info" and padrao["exames"] == ["Ferritina"]
    assert result["structured"]["idioma"] == "en"


def test_resposta_json_em_bloco_de_codigo():
    gerar, _ = _gerador(LAUDO_EN, '```json\n{"marcacoes": [{"estrutura": "ACL", "local": "mid"}]}\n```')
    assert idioma.traduzir_resultado(_resultado(), gerar)["annotations"][0]["estrutura"] == "ACL"


# ── Fluxo da análise ──────────────────────────────────────────────────────────

LAB = {"laboratorio": "Lab", "data_coleta": "2026-05-15", "resultados": [
    {"exame": "Ferritina", "valor": 346.5, "unidade": "ng/mL", "referencia_min": 23.9, "referencia_max": 336.2},
    {"exame": "Hemoglobina", "valor": 15.4, "unidade": "g/dL", "referencia_min": 13.0, "referencia_max": 17.0},
]}
INTERPRETACAO = "## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA\n- **[MODERADO]** Ferritina pouco acima."


def _fake_structured_generation(traducao_ok=True):
    chamadas = []

    def gen(gemini, model, key, images, prompt, deadline, **kw):
        chamadas.append({"prompt": prompt, **kw})
        uso = {"model": "m", "input_tokens": 10, "output_tokens": 5}
        if prompt.startswith("You are a medical translator. Translate every string"):
            itens = json.loads(prompt.split("JSON:\n", 1)[1])
            return json.dumps({"padroes": [{"titulo": "EN " + p["titulo"], "explicacao": "e", "sugestao": "s"}
                                           for p in itens.get("padroes", [])]}), uso, "claude"
        if prompt.startswith("You are a medical translator"):
            if not traducao_ok:
                raise RuntimeError("tradução fora")
            laudo = prompt.split("REPORT:\n", 1)[1]
            traduzido = (laudo.replace("IDENTIFICAÇÃO DO EXAME", "EXAM IDENTIFICATION")
                         .replace("[MODERADO]", "[MODERATE]").replace("[FORTE]", "[STRONG]"))
            return "Translated:\n" + traduzido, uso, "claude"
        if kw.get("json_mode"):
            return json.dumps(LAB), uso, "gemini"
        return INTERPRETACAO, uso, "claude"

    return gen, chamadas


def test_exame_laboratorial_em_ingles(monkeypatch):
    gen, chamadas = _fake_structured_generation()
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())
    stages = []

    result = analyzer.analyze_exam_from_bytes([(b"lab-en", "image/png")], "exames.pdf", "chave",
                                              exam_category="laboratorial", laudo_idioma="en",
                                              on_progress=stages.append)

    assert result["idioma"] == "en" and not result["traducao_falhou"]
    assert "[MODERATE]" in result["analysis"] and "[MODERADO]" not in result["analysis"]
    assert result["structured"]["idioma"] == "en"
    assert result["structured"]["padroes"][0]["titulo"].startswith("EN ")
    assert "traduzindo" in stages
    # A análise em si continua em português: só as duas últimas chamadas são da tradução.
    assert not chamadas[0]["prompt"].startswith("You are") and not chamadas[1]["prompt"].startswith("You are")
    assert len(result["usage"]) == 4


def test_portugues_nao_chama_a_traducao_e_cache_separa_idiomas(monkeypatch):
    gen, chamadas = _fake_structured_generation()
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())

    pt = analyzer.analyze_exam_from_bytes([(b"lab-cache", "image/png")], "exames.pdf", "chave",
                                          exam_category="laboratorial")
    assert pt["idioma"] == "pt-BR" and len(chamadas) == 2
    en = analyzer.analyze_exam_from_bytes([(b"lab-cache", "image/png")], "exames.pdf", "chave",
                                          exam_category="laboratorial", laudo_idioma="en")
    assert en["idioma"] == "en" and len(chamadas) == 6  # não reaproveitou o laudo em português do cache


def test_traducao_que_falha_nao_vai_para_o_cache(monkeypatch):
    gen, chamadas = _fake_structured_generation(traducao_ok=False)
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())
    args = ([(b"lab-falha", "image/png")], "exames.pdf", "chave")

    primeiro = analyzer.analyze_exam_from_bytes(*args, exam_category="laboratorial", laudo_idioma="en")
    assert primeiro["idioma"] == "pt-BR" and primeiro["traducao_falhou"] and "[MODERADO]" in primeiro["analysis"]
    analyzer.analyze_exam_from_bytes(*args, exam_category="laboratorial", laudo_idioma="en")
    assert len(chamadas) == 6  # tentou de novo em vez de devolver o português guardado


def test_correlacao_pede_o_parecer_em_ingles(monkeypatch):
    gen = MagicMock(return_value=("intro\n## 1. EXAMS CONSIDERED\n- x", None, "claude"))
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())
    result = analyzer.correlate_patient_exams([], [], [], {}, "chave", laudo_idioma="en")
    assert "inglês clínico" in gen.call_args.args[4]
    assert result["analysis"].startswith("## 1. EXAMS") and result["idioma"] == "en"


# ── Painel laboratorial e exibição ────────────────────────────────────────────

def _calc():
    return lab_tests.compute(lab_tests.parse_extraction(json.dumps({**LAB, "resultados": LAB["resultados"] + [
        {"exame": "Glicose", "valor": 90, "unidade": "mg/dL", "referencia_min": 70, "referencia_max": 99},
        {"exame": "Insulina", "valor": 12.5, "unidade": "µUI/mL", "referencia_min": 2, "referencia_max": 25},
        {"exame": "Leucócitos", "valor": 8610, "unidade": "/mm³", "referencia_min": 4000, "referencia_max": 10000,
         "anteriores": [{"data": "2024-01-31", "valor": 7000}]},
    ]})))


def test_painel_em_ingles():
    d = lab_tests.dashboard(_calc(), idioma="en")
    nomes = [p["nome"] for p in d["paineis"]]
    assert d["idioma"] == "en" and "Iron" in nomes and "Complete blood count" in nomes
    itens = {i["exame"]: i for p in d["paineis"] for i in p["itens"]}
    assert itens["Ferritina"]["rotulo"] == "↑ High" and itens["Ferritina"]["referencia"] == "23.9 to 336.2"
    assert itens["Leucócitos"]["valor"] == "8,610 /mm³"  # separador de milhar em inglês
    assert itens["Leucócitos"]["anterior"]["texto"] == "7,000 /mm³ on 2024-01-31"
    homa = d["indices"][0]
    assert homa["nome"] == "HOMA-IR (insulin resistance)" and homa["valor_fmt"] == "2.78"


def test_painel_em_portugues_continua_igual():
    d = lab_tests.dashboard(_calc())
    itens = {i["exame"]: i for p in d["paineis"] for i in p["itens"]}
    assert d["idioma"] == "pt-BR" and itens["Ferritina"]["rotulo"] == "↑ Acima"
    assert itens["Leucócitos"]["valor"] == "8.610 /mm³" and d["indices"][0]["valor_fmt"] == "2,78"


def test_painel_salvo_traduzido_abre_em_ingles():
    calc = {**_calc(), "idioma": "en"}
    assert lab_tests.dashboard(calc)["idioma"] == "en"


def test_result_view_em_ingles():
    view = presentation.result_view({
        "analysis": LAUDO_EN, "exam_type": "pe_tornozelo", "references_used": 0, "model_used": "m",
        "idioma": "en", "traducao_falhou": False,
    })
    assert view["laudo_idioma"] == "en" and view["exam_type"] == "Foot and ankle"
    assert view["consensus"]["strong_count"] == 1 and view["consensus"]["moderate_count"] == 1
    assert view["translation_failed"] is False


def test_result_view_aviso_de_traducao_que_falhou():
    view = presentation.result_view({"analysis": LAUDO_PT, "exam_type": "joelho", "references_used": 0,
                                     "model_used": "m", "idioma": "pt-BR", "traducao_falhou": True})
    assert view["laudo_idioma"] == "pt-BR" and view["translation_failed"] and view["exam_type"] == "Joelho"


def test_secao_de_sintese_em_ingles_na_comparacao():
    texto = "## 5. CLINICAL SYNTHESIS\nStable.\n## 6. NEXT STEPS\n- x"
    assert comparacao.comparar_texto("## 5. SÍNTESE CLÍNICA\nEstável.", texto)[0]["depois"] == "Stable."


# ── Banco ─────────────────────────────────────────────────────────────────────

class _FakeInsert:
    def __init__(self, client, payload):
        self.client, self.payload = client, payload

    def execute(self):
        self.client.payloads.append(dict(self.payload))
        if self.client.sem_coluna and "idioma" in self.payload:
            raise Exception("Could not find the 'idioma' column of 'analises' in the schema cache")
        return SimpleNamespace(data=[{"id": "laudo-1"}])


class _FakeDb:
    def __init__(self, sem_coluna=False):
        self.sem_coluna, self.payloads = sem_coluna, []

    def table(self, name):
        return SimpleNamespace(insert=lambda payload: _FakeInsert(self, payload))


def test_salvar_laudo_em_ingles(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    assert db.salvar_analise("joelho", LAUDO_EN, "m", 0, 1, "premium", idioma="en") == "laudo-1"
    assert fake.payloads[0]["idioma"] == "en"
    db.salvar_analise("joelho", LAUDO_PT, "m", 0, 1, "premium", idioma="pt-BR")
    assert "idioma" not in fake.payloads[1]  # português é o padrão da coluna


def test_salvar_sem_a_migration_grava_sem_o_idioma(monkeypatch):
    fake = _FakeDb(sem_coluna=True)
    monkeypatch.setattr(db, "_get_client", lambda: fake)
    assert db.salvar_analise("joelho", LAUDO_EN, "m", 0, 1, "premium", idioma="en") == "laudo-1"
    assert "idioma" in fake.payloads[0] and "idioma" not in fake.payloads[1]


def test_idioma_do_laudo_sem_a_migration_e_portugues(monkeypatch):
    def quebra(name):
        raise Exception("column analises.idioma does not exist")
    monkeypatch.setattr(db, "_get_client", lambda: SimpleNamespace(table=quebra))
    assert db.buscar_idioma_analise("laudo-1") == "pt-BR"


# ── Rotas ─────────────────────────────────────────────────────────────────────

def _login_as_admin(client):
    from web import config

    client.get(f"/admin/entrar?key={config.get_admin_key()}")


def test_rota_envia_o_idioma_escolhido(client, monkeypatch):
    analyze = MagicMock(return_value={"success": True, "exam_type": "joelho", "analysis": LAUDO_EN,
                                      "references_used": 0, "model_used": "m", "num_images": 1, "usage": [],
                                      "idioma": "en", "traducao_falhou": False})
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    monkeypatch.setattr(db, "salvar_analise_com_imagens", MagicMock(return_value="laudo-1"))
    _login_as_admin(client)

    resp = client.post("/analyze", data={"exam_image": (io.BytesIO(b"img"), "joelho.jpg"),
                                         "privacy_consent": "accepted", "laudo_idioma": "en"},
                       content_type="multipart/form-data")

    assert resp.status_code == 200 and analyze.call_args.kwargs["laudo_idioma"] == "en"
    html = resp.get_data(as_text=True)
    assert 'data-lang="en"' in html and 'lang="en"' in html and "translation-note" not in html


def test_rota_sem_escolha_e_portugues(client, monkeypatch):
    analyze = MagicMock(return_value={"success": True, "exam_type": "joelho", "analysis": LAUDO_PT,
                                      "references_used": 0, "model_used": "m", "num_images": 1, "usage": []})
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    monkeypatch.setattr(db, "salvar_analise_com_imagens", MagicMock(return_value="laudo-1"))
    _login_as_admin(client)

    resp = client.post("/analyze", data={"exam_image": (io.BytesIO(b"img"), "joelho.jpg"),
                                         "privacy_consent": "accepted"}, content_type="multipart/form-data")
    assert analyze.call_args.kwargs["laudo_idioma"] == "pt-BR"
    assert 'data-lang="pt-BR"' in resp.get_data(as_text=True)


def test_formulario_tem_o_seletor_de_idioma(client):
    _login_as_admin(client)
    html = client.get("/").get_data(as_text=True)
    assert 'name="laudo_idioma"' in html and '<option value="en">English</option>' in html


def test_correlacao_em_ingles(client, monkeypatch):
    from tests.test_exam_types import EXAMES, _Prontuario

    fake = _Prontuario()
    monkeypatch.setattr(db, "buscar_sessao_cliente", fake.buscar_sessao_cliente)
    monkeypatch.setattr(db, "buscar_paciente", fake.buscar_paciente)
    monkeypatch.setattr(db, "salvar_analise", fake.salvar_analise)
    monkeypatch.setattr(db, "listar_laudos_para_correlacao", lambda pid, cid: list(EXAMES))
    monkeypatch.setattr(db, "listar_tratamentos", lambda pid, cid: [])
    monkeypatch.setattr(db, "listar_evolucoes", lambda pid, cid: [])
    monkeypatch.setattr(db, "registrar_acesso_prontuario", lambda c, p, a: None)
    corr = MagicMock(return_value={"exam_type": "correlacao", "analysis": "## 1. EXAMS CONSIDERED\n- x",
                                   "model_used": "claude", "usage": [], "idioma": "en"})
    monkeypatch.setattr(analyzer, "correlate_patient_exams", corr)
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay", "a@x.com", 3600))

    resp = client.post("/pacientes/pac-a/correlacionar", data={"laudo_idioma": "en"})

    assert resp.status_code == 302 and corr.call_args.kwargs["laudo_idioma"] == "en"
    assert fake.salvos[0]["idioma"] == "en"


# ── Explicação para o paciente ────────────────────────────────────────────────

def test_explicacao_para_o_paciente_em_ingles(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave")
    resposta = SimpleNamespace(stop_reason="end_turn", model="claude", content=[SimpleNamespace(type="text", text="## What the exam showed\nx")],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    cliente = MagicMock()
    cliente.beta.messages.create.return_value = resposta
    monkeypatch.setattr(patient_report.anthropic, "Anthropic", lambda **k: cliente)

    texto, _ = patient_report.gerar_texto(LAUDO_EN, "en")
    kwargs = cliente.beta.messages.create.call_args.kwargs
    assert texto.startswith("## What the exam showed")
    assert kwargs["system"] == patient_report.SYSTEM_PROMPT_EN and kwargs["messages"][0]["content"].startswith("Exam report")
    patient_report.gerar_texto(LAUDO_PT)
    assert cliente.beta.messages.create.call_args.kwargs["system"] == patient_report.SYSTEM_PROMPT


# ── PDF ───────────────────────────────────────────────────────────────────────

def _pdf_html(lang):
    script = (
        "global.window = {}; require(process.argv[1]);"
        "process.stdout.write(window.buildLaudoHTML({reportHTML: '<p>r</p>', generatedAt: 'x', lang: process.argv[2],"
        "findings: [{numero: 1, imagem: 1, estrutura: 'ACL', local: 'mid', certeza: 'forte'}],"
        "images: [{src: 'data:image/png;base64,AA'}]}));"
    )
    return subprocess.run(["node", "-e", script, str(ROOT / "static" / "laudo-pdf.js"), lang],
                          capture_output=True, text=True, check=True).stdout


def test_pdf_em_ingles_tem_rotulos_e_aviso_proprios():
    html = _pdf_html("en")
    assert '<html lang="en">' in html and "AI-assisted analysis" in html and "High certainty" in html
    assert "Professional license no." in html and "GDPR" in html
    for termo in ("CFM", "COFFITO", "LGPD", "Análise de apoio", "Responsável"):
        assert termo not in html


def test_pdf_em_portugues_continua_igual():
    html = _pdf_html("pt-BR")
    assert '<html lang="pt-BR">' in html and "AVISO CFM/COFFITO/LGPD" in html and "Forte grau de certeza" in html


# ── Laudos laboratoriais em inglês (profissionais de fora do Brasil) ─────────

@pytest.mark.parametrize("exame,painel", [
    ("Ferritin", "Ferro"), ("Serum iron", "Ferro"), ("Transferrin saturation", "Ferro"),
    ("Leukocytes", "Hemograma"), ("White blood cells", "Hemograma"), ("Platelets", "Hemograma"),
    ("Mean corpuscular hemoglobin (MCH)", "Hemograma"), ("Hemoglobin A1c", "Metabolismo"),
    ("Glycated hemoglobin", "Metabolismo"), ("Hemoglobina glicada", "Metabolismo"),
    ("Creatine kinase (CK)", "Músculo e fígado"), ("Creatine phosphokinase", "Músculo e fígado"),
    ("Albumin", "Músculo e fígado"), ("Globulina", "Músculo e fígado"), ("Direct bilirubin", "Músculo e fígado"),
    ("Globulina L. de hormônios", "Hormônios"), ("Sex hormone binding globulin", "Hormônios"),
    ("C-reactive protein", "Inflamação"), ("D-dimer", "Coagulação"), ("aPTT", "Coagulação"),
    ("Vitamin D (25-OH)", "Metabolismo ósseo"), ("Alkaline phosphatase", "Metabolismo ósseo"),
    ("Creatinine", "Rim e eletrólitos"), ("Cystatin C", "Rim e eletrólitos"), ("Sodium", "Rim e eletrólitos"),
    ("Total cholesterol", "Metabolismo"), ("Apolipoprotein B", "Metabolismo"), ("Uric acid", "Metabolismo"),
    ("Free testosterone", "Hormônios"), ("Thyroxine (free T4)", "Hormônios"), ("Androstenedione", "Hormônios"),
    ("Vitamin B12", "Vitaminas"), ("Folic acid", "Vitaminas"), ("Amylase", "Pâncreas"),
    ("Prostate-specific antigen (PSA)", "Marcadores tumorais"), ("CA 19-9", "Marcadores tumorais"),
])
def test_paineis_com_nomes_em_ingles(exame, painel):
    assert lab_tests.painel(exame) == painel


def _titulos_en(resultados):
    calc = lab_tests.compute(lab_tests.parse_extraction(json.dumps({"resultados": resultados})))
    return [p["titulo"] for p in calc["padroes"]], calc


def _r(exame, valor, unidade, minimo=None, maximo=None):
    return {"exame": exame, "valor": valor, "unidade": unidade, "referencia_min": minimo, "referencia_max": maximo}


def test_padroes_com_nomes_em_ingles():
    titulos, calc = _titulos_en([
        _r("Hemoglobin", 12.1, "g/dL", 13.5, 17.5), _r("Ferritin", 12, "ng/mL", 30, 400),
        _r("Creatine kinase", 900, "U/L", None, 190), _r("AST", 80, "U/L", None, 40), _r("ALT", 30, "U/L", None, 41),
        _r("Vitamin D (25-OH)", 15, "ng/mL", 30, 100), _r("LDL cholesterol", 160, "mg/dL", None, 130),
        _r("Glucose", 110, "mg/dL", 70, 99), _r("Insulin", 20, "µIU/mL", 2, 25),
        _r("Serum iron", 40, "µg/dL", 65, 175), _r("Transferrin", 300, "mg/dL", 200, 360),
    ])
    assert "Compatível com anemia por deficiência de ferro" in titulos
    assert "Compatível com lesão ou sobrecarga muscular (CK com transaminases)" in titulos
    assert "Vitamina D deficiente" in titulos and "Colesterol LDL elevado" in titulos
    assert "Sinais de resistência à insulina" in titulos
    nomes = {i["nome"] for i in calc["indices"]}
    assert {"HOMA-IR (resistência à insulina)", "Saturação de transferrina (estimada)"} <= nomes


def test_ck_mb_nao_conta_como_ck_total():
    titulos, _ = _titulos_en([_r("CK-MB", 30, "U/L", None, 25), _r("Creatine kinase", 150, "U/L", None, 190)])
    assert not any("CK" in t or "muscular" in t for t in titulos)
