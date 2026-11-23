"""Leitura incerta da extração: marcada em amarelo, nunca passa em silêncio."""

import json

from flask import render_template

from core import dynamometry, lab_tests


def _r(exame, valor, unidade="mg/dL", mn=None, mx=None, duvida=None):
    return {"exame": exame, "valor": valor, "valor_texto": "", "unidade": unidade, "referencia_min": mn,
            "referencia_max": mx, "referencia_texto": "", "anteriores": [], "duvida": duvida}


def _calc(*resultados):
    return lab_tests.compute({"resultados": list(resultados), "laboratorio": "", "data_coleta": None, "paciente": {}})


def test_duvida_declarada_pela_ia_chega_ao_resultado():
    texto = json.dumps({"resultados": [{"exame": "CK", "valor": 2.5, "unidade": "U/L", "duvida": "dígito final ilegível"}]})
    dados = lab_tests.parse_extraction(texto)
    assert dados["resultados"][0]["duvida"] == "dígito final ilegível"
    calc = lab_tests.compute(dados)
    assert calc["conferir"] == 1 and "ilegível" in calc["resultados"][0]["conferir"]


def test_checagens_deterministicas():
    assert lab_tests.conferir_leitura(_r("Hemoglobina", 0.13, "g/dL", 12, 16)).startswith("valor muito distante")
    assert lab_tests.conferir_leitura(_r("X", 5, "mg/dL", 10, 2)) == "faixa de referência invertida"
    assert lab_tests.conferir_leitura(_r("Glicose", 90, "", 70, 99)) == "sem unidade"
    # Valores extremos mas reais não são marcados: CK de rabdomiólise, PCR muito baixa.
    assert lab_tests.conferir_leitura(_r("CK", 48000, "U/L", 30, 200)) is None
    assert lab_tests.conferir_leitura(_r("PCR", 0.02, "mg/dL", None, 0.5)) is None


def test_tabela_do_laudo_avisa_para_conferir():
    calc = _calc(_r("Ferritina", 45, "ng/mL", 30, 300, duvida="unidade cortada"), _r("Glicose", 90, "mg/dL", 70, 99))
    tabela = lab_tests.format_results(calc)
    linha_ferritina = next(linha for linha in tabela.splitlines() if "Ferritina" in linha)
    linha_glicose = next(linha for linha in tabela.splitlines() if "Glicose" in linha)
    assert "conferir no laudo original" in linha_ferritina and "conferir" not in linha_glicose


def test_painel_mostra_amarelo_filtro_e_recolhe_paineis_normais(client):
    calc = _calc(
        _r("Ferritina", 45, "ng/mL", 30, 300, duvida="unidade cortada"),
        _r("Glicose", 90, "mg/dL", 70, 99), _r("Colesterol total", 150, "mg/dL", None, 190),
        _r("LDL", 160, "mg/dL", None, 130), _r("TSH", 2, "uUI/mL", 0.4, 4), _r("Creatinina", 1, "mg/dL", 0.6, 1.2),
    )
    with client.application.test_request_context():
        html = render_template("_painel_laboratorial.html", lab_dashboard=lab_tests.dashboard(calc))
    assert "lab-row lab-normal lab-check" in html and "conferir: unidade cortada" in html
    assert "para conferir" in html and "Só alterados" in html
    assert 'class="lab-panel is-normal"' in html  # painel todo na faixa vem recolhido
    assert "<details" in html and "</details>" in html


def test_dinamometria_lado_improvavel():
    dados = dynamometry.compute({
        "lado_acometido": None, "massa_corporal_kg": None, "plataforma": [],
        "medidas": [{"grupo_muscular": "Quadríceps", "acao": "concentrica", "velocidade_graus_s": 60,
                     "angulo_graus": None, "unidade": "Nm", "pico_direito": 210, "pico_esquerdo": 21.0}],
    })
    assert "3×" in dados["medidas"][0]["conferir"]
    assert "conferir no relatório original" in dynamometry.format_results(dados)
