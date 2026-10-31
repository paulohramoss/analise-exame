"""
Dinamometria (isocinética, isométrica, plataforma de força), exames laboratoriais
e correlação dos exames no prontuário.

Os cálculos são conferidos à mão: nenhum número do laudo pode depender da IA.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import analyzer, db, dynamometry, lab_tests
from web import prontuario, security

ROOT = Path(__file__).resolve().parents[1]

ISOCINETICO = {
    "tipo_teste": "isocinetico", "aparelho": "Biodex System 4", "protocolo": "Joelho 60°/s e 180°/s",
    "massa_corporal_kg": "78,5", "lado_acometido": "esquerdo",
    "medidas": [
        {"grupo_muscular": "Quadríceps", "acao": "concêntrica", "velocidade_graus_s": 60, "unidade": "Nm",
         "pico_direito": "245,3", "pico_esquerdo": 198.1, "trabalho_direito": 230, "trabalho_esquerdo": 180,
         "fadiga_direito_pct": 18, "fadiga_esquerdo_pct": 29},
        {"grupo_muscular": "Isquiotibiais", "acao": "concentrica", "velocidade_graus_s": 60, "unidade": "Nm",
         "pico_direito": 140, "pico_esquerdo": 104},
        {"grupo_muscular": "Isquiotibiais", "acao": "excentrica", "velocidade_graus_s": 60, "unidade": "Nm",
         "pico_direito": 190, "pico_esquerdo": 160},
        {"grupo_muscular": "Quadríceps", "acao": "concentrica", "velocidade_graus_s": 180, "unidade": "Nm",
         "pico_direito": 160, "pico_esquerdo": 150},
    ],
}

LABORATORIAL = {
    "laboratorio": "Laboratório Exemplo", "data_coleta": "2026-10-20",
    "resultados": [
        {"exame": "Creatinoquinase (CK)", "valor": "1.250", "unidade": "U/L", "referencia_max": 190,
         "referencia_texto": "até 190 U/L"},
        {"exame": "Hemoglobina", "valor": 13.1, "unidade": "g/dL", "referencia_min": 13.5, "referencia_max": 17.5},
        {"exame": "Hemoglobina glicada (HbA1c)", "valor": 5.2, "unidade": "%", "referencia_max": 5.7},
        {"exame": "Vitamina D (25-OH)", "valor": "18,4", "unidade": "ng/mL", "referencia_min": 30},
        {"exame": "Ferritina", "valor": 22, "unidade": "ng/mL", "referencia_min": 30, "referencia_max": 400},
        {"exame": "HIV", "valor_texto": "Não reagente"},
    ],
}


def _dyn(**override):
    return dynamometry.compute(dynamometry.parse_extraction(json.dumps({**ISOCINETICO, **override})))


# ── Dinamometria: leitura e cálculos ──────────────────────────────────────────

def test_leitura_aceita_virgula_decimal_e_milhar():
    calc = _dyn()
    quad = calc["medidas"][0]
    assert quad["pico_direito"] == 245.3 and calc["massa_corporal_kg"] == 78.5
    assert quad["acao"] == "concentrica" and calc["lado_acometido"] == "esquerdo"
    assert dynamometry._number("1.234,5") == 1234.5


@pytest.mark.parametrize("texto,lado", [
    ("Esquerdo", "esquerdo"), ("D", "direito"), ("right", "direito"), ("desconhecido", None), ("", None),
])
def test_lado_acometido(texto, lado):
    assert dynamometry._side(texto) == lado


def test_simetria_com_lado_acometido():
    pico = _dyn()["medidas"][0]["pico"]  # 198,1 / 245,3
    assert pico == {"lsi_pct": 80.8, "deficit_pct": 19.2, "lado_mais_fraco": "esquerdo",
                    "classificacao": "Assimetria moderada", "criterio_retorno": False}


def test_simetria_sem_lado_acometido_usa_o_mais_fraco():
    assert dynamometry.bilateral(100, 120, None)["lado_mais_fraco"] == "direito"
    assert dynamometry.bilateral(100, 120, None)["deficit_pct"] == 16.7


def test_lado_acometido_mais_forte_nao_gera_deficit():
    resultado = dynamometry.bilateral(100, 110, "esquerdo")  # acometido 110 / outro 100
    assert resultado["lsi_pct"] == 110.0 and resultado["deficit_pct"] == 0 and resultado["criterio_retorno"]


@pytest.mark.parametrize("deficit,classe", [
    (5, "Simétrico"), (10, "Assimetria leve"), (14.9, "Assimetria leve"), (15, "Assimetria moderada"),
    (20, "Assimetria importante"),
])
def test_classificacao_do_deficit(deficit, classe):
    assert dynamometry.classify_deficit(deficit) == classe


def test_torque_por_peso():
    norm = _dyn()["medidas"][0]["normalizado"]
    assert norm == {"unidade": "Nm/kg", "direito": 3.12, "esquerdo": 2.52}  # 245,3/78,5 e 198,1/78,5
    assert "normalizado" not in _dyn(massa_corporal_kg=None)["medidas"][0]


def test_relacoes_isquiotibiais_quadriceps():
    relacoes = {r["relacao"]: r for r in _dyn()["relacoes"]}
    conv = relacoes["Isquiotibiais:Quadríceps convencional"]
    func = relacoes["Isquiotibiais:Quadríceps funcional"]
    assert (conv["direito"], conv["esquerdo"]) == (0.57, 0.52)  # 140/245,3 e 104/198,1
    assert (func["direito"], func["esquerdo"]) == (0.77, 0.81)  # 190/245,3 e 160/198,1
    assert conv["abaixo_da_referencia"] == ["direito", "esquerdo"]
    assert all(r["velocidade_graus_s"] == 60 for r in relacoes.values())  # 180°/s não tem isquio para parear


def test_rotadores_do_ombro():
    calc = dynamometry.compute(dynamometry.parse_extraction(json.dumps({
        "tipo_teste": "isometrico", "medidas": [
            {"grupo_muscular": "Rotadores externos do ombro", "unidade": "N", "pico_direito": 120, "pico_esquerdo": 90},
            {"grupo_muscular": "Rotadores internos do ombro", "unidade": "N", "pico_direito": 160, "pico_esquerdo": 150},
        ]})))
    [rel] = calc["relacoes"]
    assert rel["relacao"] == "Rotadores externos:internos" and (rel["direito"], rel["esquerdo"]) == (0.75, 0.6)
    assert rel["abaixo_da_referencia"] == ["esquerdo"]
    assert calc["medidas"][0]["acao"] == "isometrica"


def test_plataforma_de_forca():
    calc = dynamometry.compute(dynamometry.parse_extraction(json.dumps({
        "tipo_teste": "plataforma", "plataforma": [
            {"teste": "CMJ", "metrica": "Pico de força", "unidade": "N", "esquerdo": 980, "direito": 1150},
            {"teste": "CMJ", "metrica": "Altura do salto", "unidade": "cm", "valor": 34.2},
        ]})))
    assert calc["tipo_teste"] == "plataforma"
    assim = calc["plataforma"][0]
    assert assim["assimetria_pct"] == 14.8 and assim["lado_menor"] == "esquerdo" and assim["assimetria_relevante"]
    assert "assimetria_pct" not in calc["plataforma"][1]


def test_relatorio_sem_valores_e_recusado():
    with pytest.raises(ValueError):
        dynamometry.parse_extraction(json.dumps({"tipo_teste": "isocinetico", "medidas": [{"grupo_muscular": "Quadríceps"}]}))
    with pytest.raises(ValueError):
        dynamometry.parse_extraction("não é json")


def test_tabelas_do_laudo_de_dinamometria():
    calc = _dyn()
    texto = dynamometry.format_identification(calc) + "\n" + dynamometry.format_results(calc)
    assert "Dinamometria isocinética" in texto and "Biodex System 4" in texto and "78,5 kg" in texto
    assert "| Quadríceps | concêntrica 60°/s | 245,3 Nm | 198,1 Nm | 3,12 / 2,52 Nm/kg | 80,8% | 19,2% (esquerdo) | Assimetria moderada |" in texto
    assert "| Isquiotibiais:Quadríceps convencional | 60°/s | 0,57 | 0,52 | ≥ 0,60 (abaixo: direito, esquerdo) |" in texto
    assert "21,7%" in texto  # déficit de trabalho 180/230


def test_laudo_sem_interpretacao_mantem_as_tabelas():
    laudo = dynamometry.compose_report(_dyn(), None)
    assert laudo.startswith("## 1. IDENTIFICAÇÃO DO EXAME") and "## 2. RESULTADOS" in laudo
    assert "Interpretação automática indisponível" in laudo


# ── Exames laboratoriais ──────────────────────────────────────────────────────

def _lab():
    return lab_tests.compute(lab_tests.parse_extraction(json.dumps(LABORATORIAL)))


def test_situacao_pela_faixa_do_proprio_laudo():
    por_exame = {r["exame"]: r for r in _lab()["resultados"]}
    assert por_exame["Creatinoquinase (CK)"]["situacao"] == "acima"
    assert por_exame["Creatinoquinase (CK)"]["vezes_o_limite"] == 6.6
    assert por_exame["Hemoglobina"]["situacao"] == "abaixo"
    assert por_exame["Vitamina D (25-OH)"]["situacao"] == "abaixo"
    assert por_exame["Hemoglobina glicada (HbA1c)"]["situacao"] == "normal"
    assert por_exame["HIV"]["situacao"] == "sem_referencia"


def test_paineis():
    assert lab_tests.painel("Hemoglobina glicada (HbA1c)") == "Metabolismo"
    assert lab_tests.painel("Hemoglobina") == "Hemograma"
    assert lab_tests.painel("Ferritina") == "Ferro"
    assert lab_tests.painel("Creatinoquinase (CK)") == "Músculo"
    assert lab_tests.painel("Creatinina") == "Rim e eletrólitos"


def test_tabela_do_laudo_laboratorial():
    calc = _lab()
    texto = lab_tests.format_identification(calc) + "\n" + lab_tests.format_results(calc)
    assert "6 (4 fora da faixa de referência do laudo)" in texto
    assert "| Músculo | Creatinoquinase (CK) | 1250 U/L | até 190 U/L | **↑ Acima (6,6× o limite)** |" in texto
    assert "| Hemograma | Hemoglobina | 13,1 g/dL | 13,5 a 17,5 | **↓ Abaixo** |" in texto
    assert "| Outros | HIV | Não reagente | — | Sem referência |" in texto


def test_laudo_laboratorial_sem_resultados_e_recusado():
    with pytest.raises(ValueError):
        lab_tests.parse_extraction(json.dumps({"resultados": []}))


# ── Detecção do tipo e fluxo no analisador ────────────────────────────────────

@pytest.mark.parametrize("nome,texto,categoria", [
    ("relatorio.pdf", "Biodex - Pico de torque 60°/s, déficit 18%", "dinamometria"),
    ("forcedecks.pdf", "CMJ altura do salto, RFD, assimetria", "dinamometria"),
    ("exames.pdf", "Hemograma completo. Creatinina 1,0 mg/dL. Valores de referência", "laboratorial"),
    ("joelho.jpg", "RM do joelho, dor medial", "imagem"),
    ("rm.jpg", "RM do joelho, paciente com creatinina normal", "imagem"),  # um termo só não basta
])
def test_deteccao_do_tipo_de_exame(nome, texto, categoria):
    assert analyzer.detect_exam_category(nome, texto) == categoria


def _fake_generate(respostas):
    chamadas = []

    def _gen(gemini, model, anthropic_key, images, prompt, deadline, **kwargs):
        chamadas.append({"prompt": prompt, "imagens": len(images), **kwargs})
        resposta = respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta, {"model": "m", "input_tokens": 10, "output_tokens": 5}, "modelo-x"

    return _gen, chamadas


INTERPRETACAO = "texto antes\n## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA\n- **[FORTE]** déficit de quadríceps"


def test_fluxo_estruturado_le_calcula_e_interpreta(monkeypatch):
    gen, chamadas = _fake_generate([json.dumps(ISOCINETICO), INTERPRETACAO])
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)

    result = analyzer._analyze_structured_exam(
        "dinamometria", None, "m", "chave", [(b"pagina", "image/jpeg")], "Texto do PDF: Biodex", 1e18,
    )

    assert chamadas[0]["json_mode"] and chamadas[0]["imagens"] == 1 and "Texto do PDF: Biodex" in chamadas[0]["prompt"]
    assert chamadas[1]["prefer_claude"] and chamadas[1]["imagens"] == 0
    assert '"deficit_pct": 19.2' in chamadas[1]["prompt"]  # a interpretação recebe os valores já calculados
    assert result["exam_type"] == "dinamometria" and result["annotations"] == []
    assert "| Quadríceps | concêntrica 60°/s |" in result["analysis"]
    assert result["analysis"].endswith("- **[FORTE]** déficit de quadríceps") and "texto antes" not in result["analysis"]
    assert result["structured"]["medidas"][0]["pico"]["deficit_pct"] == 19.2
    assert len(result["usage"]) == 2 and result["model_used"] == "modelo-x (leitura) + modelo-x (interpretação)"


def test_interpretacao_falha_e_o_laudo_sai_com_as_tabelas(monkeypatch):
    gen, _ = _fake_generate([json.dumps(LABORATORIAL), RuntimeError("503")])
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    result = analyzer._analyze_structured_exam("laboratorial", None, "m", "chave", [(b"p", "image/jpeg")], "", 1e18)
    assert "## 2. RESULTADOS E SITUAÇÃO" in result["analysis"] and "Interpretação automática indisponível" in result["analysis"]


def test_arquivo_que_nao_e_do_tipo_escolhido(monkeypatch):
    gen, _ = _fake_generate([json.dumps({"medidas": []})])
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    with pytest.raises(RuntimeError, match="não parecem resultados de dinamometria"):
        analyzer._analyze_structured_exam("dinamometria", None, "m", "chave", [(b"p", "image/jpeg")], "", 1e18)


def test_pedido_de_exames_em_vez_do_resultado(monkeypatch):
    """Caso real de teste: o PDF enviado era a solicitação da nutricionista, sem valores."""
    gen, _ = _fake_generate([json.dumps({"resultados": [{"exame": "Ferritina", "valor": None}]})])
    monkeypatch.setattr(analyzer, "_generate_structured_text", gen)
    texto_do_pdf = "Texto do PDF enviado: SOLICITAÇÃO DE EXAME LABORATORIAL Solicito exame de sangue: -Hemograma -Ferritina"
    with pytest.raises(RuntimeError, match="pedido de exames, não o resultado"):
        analyzer._analyze_structured_exam("laboratorial", None, "m", "chave", [(b"p", "image/jpeg")], texto_do_pdf, 1e18)


def test_mensagem_de_leitura_chega_a_tela(client, monkeypatch):
    import io

    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", MagicMock(
        side_effect=analyzer.ExamReadError("O arquivo parece ser um pedido de exames, não o resultado.")))
    resp = client.post("/trial/analyze", data={"exam_image": (io.BytesIO(b"img"), "pedido.jpg"),
                                               "privacy_consent": "accepted"}, content_type="multipart/form-data")
    assert resp.status_code == 422 and "pedido de exames" in resp.get_json()["error"]


def test_mensagem_de_leitura_chega_pela_fila(client, monkeypatch, fake_redis_conn, run_pending_jobs):
    import io

    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", MagicMock(
        side_effect=analyzer.ExamReadError("O arquivo parece ser um pedido de exames, não o resultado.")))
    resp = client.post("/trial/analyze", data={"exam_image": (io.BytesIO(b"img"), "pedido.jpg"),
                                               "privacy_consent": "accepted"}, content_type="multipart/form-data")
    job_id = resp.get_json()["job_id"]
    run_pending_jobs(fake_redis_conn)
    from core import jobs

    assert "pedido de exames" in jobs.get_job_status(job_id)["error"]


def test_analise_estruturada_nao_passa_pelo_laudo_de_imagem(monkeypatch):
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())
    structured = MagicMock(return_value={"exam_type": "laboratorial", "analysis": "x", "usage": []})
    monkeypatch.setattr(analyzer, "_analyze_structured_exam", structured)
    monkeypatch.setattr(analyzer, "_produce_final_analysis", MagicMock(side_effect=AssertionError("laudo de imagem")))
    result = analyzer.analyze_exam_from_bytes([(b"img-lab", "image/png")], "exames.pdf", "chave", exam_category="laboratorial")
    assert result["exam_type"] == "laboratorial" and structured.call_args.args[0] == "laboratorial"


def test_provedor_de_reserva(monkeypatch):
    gemini = MagicMock(side_effect=RuntimeError("gemini fora"))
    monkeypatch.setattr(analyzer, "_generate_with_retry", gemini)
    message = MagicMock(stop_reason="end_turn", usage=None)
    message.content = [MagicMock(text="resposta do claude")]
    client = MagicMock()
    client.messages.create.return_value = message
    monkeypatch.setattr(analyzer, "_make_anthropic_client", lambda *a, **k: client)
    texto, _, modelo = analyzer._generate_structured_text(object(), "gemini", "chave", [], "p", 1e18,
                                                          prefer_claude=False, json_mode=True)
    assert texto == "resposta do claude" and modelo == analyzer._CLAUDE_MODEL and gemini.called


# ── Rotas de análise ──────────────────────────────────────────────────────────

def _login_as_admin(client):
    from web import config

    client.get(f"/admin/entrar?key={config.get_admin_key()}")


def test_rota_envia_o_tipo_escolhido(client, monkeypatch):
    import io

    analyze = MagicMock(return_value={"success": True, "exam_type": "dinamometria", "analysis": "## 1. x",
                                      "references_used": 0, "model_used": "m", "num_images": 1, "usage": []})
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    salvar = MagicMock(return_value="laudo-1")
    monkeypatch.setattr(db, "salvar_analise_com_imagens", salvar)
    _login_as_admin(client)

    resp = client.post("/analyze", data={"exam_image": (io.BytesIO(b"img"), "relatorio.jpg"),
                                         "privacy_consent": "accepted", "exam_category": "dinamometria"},
                       content_type="multipart/form-data")

    assert resp.status_code == 200
    assert analyze.call_args.kwargs["exam_category"] == "dinamometria"
    assert salvar.call_args.kwargs["modalidade"] == "Dinamometria"
    assert "findings-block" not in resp.get_data(as_text=True)  # sem círculos em exame que não é de imagem


def test_rota_detecta_exame_de_sangue_pelo_texto_do_pdf(client, monkeypatch):
    import io

    from tests.test_pdf_uploads import pdf_com_texto

    analyze = MagicMock(return_value={"success": True, "exam_type": "laboratorial", "analysis": "x",
                                      "references_used": 0, "model_used": "m", "num_images": 1, "usage": []})
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    _login_as_admin(client)
    pdf = pdf_com_texto(["Hemograma completo", "Hemoglobina 13,1 g/dL  Valores de referencia: 13,5 a 17,5",
                         "Creatinina 1,0 mg/dL"])

    client.post("/analyze", data={"exam_images": [(io.BytesIO(pdf), "resultado.pdf")], "privacy_consent": "accepted"},
                content_type="multipart/form-data")

    assert analyze.call_args.kwargs["exam_category"] == "laboratorial"


def test_salva_os_dados_estruturados(monkeypatch):
    salvar = MagicMock(return_value="laudo-1")
    monkeypatch.setattr(db, "salvar_analise", salvar)
    monkeypatch.setattr(db, "salvar_imagem_exame", MagicMock())
    db.salvar_analise_com_imagens({"exam_type": "dinamometria", "analysis": "x", "model_used": "m", "references_used": 0,
                                   "structured": {"medidas": []}}, "premium", [(b"a", "image/jpeg")], [{}])
    assert salvar.call_args.kwargs["dados_estruturados"] == {"medidas": []}


def test_renderizador_desenha_tabela_e_escapa_html():
    script = (
        "global.window = {}; require(process.argv[1]);"
        "process.stdout.write(window.renderLaudoMarkdown('| A | B |\\n|---|---|\\n| **x** <img src=y> | 1,0 |\\nfim'));"
    )
    html = subprocess.run(["node", "-e", script, str(ROOT / "static" / "laudo-ui.js")],
                          capture_output=True, text=True, check=True).stdout
    assert '<table class="laudo-table"><thead><tr><th>A</th><th>B</th></tr></thead>' in html
    assert "<td><strong>x</strong> &lt;img src=y&gt;</td>" in html and "<p>fim</p>" in html


# ── Correlação no prontuário ──────────────────────────────────────────────────

EXAMES = [
    {"id": "l2", "created_at": "2026-10-20T10:00:00+00:00", "tipo_exame": "dinamometria", "modalidade": "Dinamometria",
     "analise_completa": "## 2. RESULTADOS\n| Quadríceps | déficit 19,2% |"},
    {"id": "l1", "created_at": "2026-09-01T10:00:00+00:00", "tipo_exame": "joelho", "modalidade": "RM",
     "analise_completa": "## 3. ACHADOS\n- [FORTE] rotura do LCA\n\n## 7. REFERÊNCIAS CIENTÍFICAS (PubMed)\n- artigo"},
]


def test_prompt_de_correlacao():
    prompt = analyzer.build_correlation_prompt(
        EXAMES, [{"tipo": "Fisioterapia", "data_inicio": "2026-09-05", "regiao": "joelho"}],
        [{"data": "2026-10-01", "dor_eva": 3, "funcao": 70, "nota": "melhora"}], {"idade": 32, "sexo": "Feminino"},
    )
    assert prompt.index("EXAME 1 — 2026-09-01") < prompt.index("EXAME 2 — 2026-10-20")  # ordem cronológica
    assert "Imagem (RM) — região: joelho" in prompt and "— Dinamometria" in prompt
    assert "REFERÊNCIAS CIENTÍFICAS" not in prompt and "rotura do LCA" in prompt
    assert "32 anos, Feminino" in prompt and "Fisioterapia (joelho)" in prompt and "dor (EVA) 3/10" in prompt
    assert "## 2. CORRELAÇÃO ENTRE OS ACHADOS" in prompt


class _Prontuario:
    def __init__(self):
        self.salvos, self.acessos = [], []

    def buscar_sessao_cliente(self, email):
        return {"id": {"a@x.com": "cli-a", "b@x.com": "cli-b"}[email], "sessao_versao": 0}

    def buscar_paciente(self, paciente_id, cliente_id):
        if paciente_id == "pac-a" and cliente_id == "cli-a":
            return {"id": "pac-a", "nome": "Maria da Silva", "data_nascimento": "1994-01-01", "sexo": "Feminino"}
        return None

    def salvar_analise(self, **kwargs):
        self.salvos.append(kwargs)
        return "laudo-correlacao"


@pytest.fixture()
def correlacao(monkeypatch):
    fake = _Prontuario()
    monkeypatch.setattr(db, "buscar_sessao_cliente", fake.buscar_sessao_cliente)
    monkeypatch.setattr(db, "buscar_paciente", fake.buscar_paciente)
    monkeypatch.setattr(db, "salvar_analise", fake.salvar_analise)
    monkeypatch.setattr(db, "listar_laudos_para_correlacao", lambda pid, cid: list(EXAMES))
    monkeypatch.setattr(db, "listar_tratamentos", lambda pid, cid: [])
    monkeypatch.setattr(db, "listar_evolucoes", lambda pid, cid: [
        {"data": "2026-10-01", "dor_eva": 3, "nota": "Paciente: Maria da Silva relata melhora"}])
    monkeypatch.setattr(db, "registrar_acesso_prontuario", lambda c, p, a: fake.acessos.append(a))
    corr = MagicMock(return_value={"exam_type": "correlacao", "analysis": "## 1. EXAMES CONSIDERADOS\n- x",
                                   "model_used": "claude", "usage": []})
    monkeypatch.setattr(analyzer, "correlate_patient_exams", corr)
    fake.corr = corr
    return fake


def _login(client, email):
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay", email, 3600))


def test_correlacao_salva_o_parecer_no_prontuario(client, correlacao):
    _login(client, "a@x.com")
    resp = client.post("/pacientes/pac-a/correlacionar")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/laudos/laudo-correlacao")
    [salvo] = correlacao.salvos
    assert salvo["tipo_exame"] == "correlacao" and salvo["paciente_id"] == "pac-a" and salvo["cliente_id"] == "cli-a"
    args = correlacao.corr.call_args.args
    assert args[3] == {"idade": prontuario.idade("1994-01-01"), "sexo": "Feminino"}
    assert "Maria" not in str(args)  # nome do paciente não vai para a IA, nem pela nota da evolução
    assert "correlacionar" in correlacao.acessos


def test_correlacao_exige_dois_exames(client, correlacao, monkeypatch):
    monkeypatch.setattr(db, "listar_laudos_para_correlacao", lambda pid, cid: EXAMES[:1])
    _login(client, "a@x.com")
    resp = client.post("/pacientes/pac-a/correlacionar", follow_redirects=True)
    assert "pelo menos dois exames" in resp.get_data(as_text=True)
    correlacao.corr.assert_not_called()


def test_correlacao_de_paciente_de_outro_profissional(client, correlacao):
    _login(client, "b@x.com")
    assert client.post("/pacientes/pac-a/correlacionar").status_code == 404
    correlacao.corr.assert_not_called()


def test_linha_do_tempo_mostra_o_parecer():
    eventos = prontuario.linha_do_tempo(
        [{"id": "c", "created_at": "2026-10-21T10:00:00+00:00", "tipo_exame": "correlacao"},
         {"id": "d", "created_at": "2026-10-20T10:00:00+00:00", "tipo_exame": "dinamometria"}], [], [])
    assert [e["titulo"] for e in eventos] == ["Parecer: correlação dos exames", "Exame: Dinamometria"]
