"""
Marcação dos achados nas imagens: pedido ao modelo, validação da resposta,
chamada no fluxo da análise e persistência/exibição das marcações.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import analyzer, annotations, db
from web import presentation

RESPOSTA = {"achados": [
    {"imagem": 1, "estrutura": "Menisco medial", "local": "corno posterior do menisco medial",
     "certeza": "moderado", "box_2d": [520, 300, 610, 470]},
    {"imagem": 2, "estrutura": "Côndilo femoral", "local": "edema subcondral no côndilo lateral",
     "certeza": "baixo", "box_2d": [200, 550, 330, 700]},
    {"imagem": 1, "estrutura": "LCA", "local": "rotura completa do ligamento cruzado anterior",
     "certeza": "forte", "box_2d": [300, 420, 520, 560]},
]}


def test_prompt_pede_certeza_do_laudo_e_coordenadas_normalizadas():
    prompt = annotations.build_localization_prompt("## 3. ACHADOS\n[FORTE] rotura do LCA", 2)
    assert '"forte" para achados marcados [FORTE]' in prompt
    assert '"baixo" para os achados listados como descartados' in prompt
    assert "0 a 1000" in prompt and "1 a 2" in prompt
    assert "[FORTE] rotura do LCA" in prompt


def test_parse_ordena_por_certeza_e_numera():
    marcacoes = annotations.parse_localization(json.dumps(RESPOSTA), num_images=2)
    assert [(m["numero"], m["certeza"], m["estrutura"]) for m in marcacoes] == [
        (1, "forte", "LCA"), (2, "moderado", "Menisco medial"), (3, "baixo", "Côndilo femoral"),
    ]
    assert marcacoes[0]["box_2d"] == [300, 420, 520, 560]


def test_parse_aceita_bloco_markdown_e_sinonimos():
    texto = "Segue:\n```json\n" + json.dumps({"achados": [
        {"imagem": 1, "estrutura": "Patela", "local": "polo inferior", "certeza": "Alta", "box_2d": [1, 2, 300, 400]},
        {"imagem": 1, "estrutura": "Bursa", "local": "pré-patelar", "certeza": "fraca", "box_2d": [10, 20, 300, 400]},
    ]}) + "\n```"
    assert [m["certeza"] for m in annotations.parse_localization(texto, 1)] == ["forte", "baixo"]


@pytest.mark.parametrize("item", [
    {"imagem": 3, "estrutura": "X", "certeza": "forte", "box_2d": [0, 0, 100, 100]},     # imagem inexistente
    {"imagem": 1, "estrutura": "X", "certeza": "talvez", "box_2d": [0, 0, 100, 100]},    # certeza desconhecida
    {"imagem": 1, "estrutura": "", "certeza": "forte", "box_2d": [0, 0, 100, 100]},      # sem estrutura
    {"imagem": 1, "estrutura": "X", "certeza": "forte", "box_2d": [0, 0, 100]},          # caixa incompleta
    {"imagem": 1, "estrutura": "X", "certeza": "forte", "box_2d": [500, 500, 500, 900]},  # caixa degenerada
    {"imagem": "a", "estrutura": "X", "certeza": "forte", "box_2d": [0, 0, 100, 100]},
    "texto solto",
])
def test_parse_descarta_itens_invalidos(item):
    assert annotations.parse_localization(json.dumps({"achados": [item]}), num_images=2) == []


def test_parse_limita_coordenadas_e_texto():
    item = {"imagem": 1, "estrutura": "E" * 500, "local": "", "certeza": "forte", "box_2d": [-50, 10, 1400, 900]}
    [marcacao] = annotations.parse_localization(json.dumps([item]), 1)
    assert marcacao["box_2d"] == [0, 10, 1000, 900]
    assert len(marcacao["estrutura"]) == 240 and marcacao["local"] == marcacao["estrutura"]


@pytest.mark.parametrize("texto", ["", "não sei", "{quebrado", json.dumps({"outro": 1})])
def test_parse_resposta_ilegivel_vira_lista_vazia(texto):
    assert annotations.parse_localization(texto, 1) == []


def test_parse_limita_quantidade(monkeypatch):
    monkeypatch.setattr(annotations, "MAX_MARCACOES", 2)
    assert len(annotations.parse_localization(json.dumps(RESPOSTA), 2)) == 2


# ── Chamada ao modelo ─────────────────────────────────────────────────────────

def test_locate_findings_usa_gemini_em_json(monkeypatch):
    generate = MagicMock(return_value=SimpleNamespace(text=json.dumps(RESPOSTA), usage_metadata=None))
    monkeypatch.setattr(analyzer, "_generate_with_retry", generate)

    marcacoes, _ = analyzer._locate_findings(
        object(), "gemini-2.5-flash", "", [(b"a", "image/png"), (b"b", "image/png")], "laudo",
    )

    assert len(marcacoes) == 3
    assert generate.call_args.kwargs["response_mime_type"] == "application/json"


def test_locate_findings_cai_para_claude_quando_gemini_falha(monkeypatch):
    monkeypatch.setattr(analyzer, "_generate_with_retry", MagicMock(side_effect=RuntimeError("503")))
    message = SimpleNamespace(content=[SimpleNamespace(text=json.dumps(RESPOSTA))], usage=None)
    client = MagicMock()
    client.messages.create.return_value = message
    monkeypatch.setattr(analyzer, "_make_anthropic_client", lambda *a, **k: client)
    monkeypatch.setattr(analyzer, "_extract_claude_usage", lambda model, msg: None)

    marcacoes, _ = analyzer._locate_findings(
        object(), "gemini-2.5-flash", "chave-anthropic", [(b"a", "image/png"), (b"b", "image/png")], "laudo",
    )

    assert [m["estrutura"] for m in marcacoes] == ["LCA", "Menisco medial", "Côndilo femoral"]


def test_locate_findings_nunca_derruba_o_laudo(monkeypatch):
    monkeypatch.setattr(analyzer, "_generate_with_retry", MagicMock(side_effect=RuntimeError("boom")))
    assert analyzer._locate_findings(object(), "m", "", [(b"a", "image/png")], "laudo") == ([], None)


def test_analise_inclui_marcacoes_quando_ligado(monkeypatch):
    monkeypatch.setattr(analyzer, "_ANNOTATIONS_ENABLED", True)
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())
    monkeypatch.setattr(analyzer, "detect_exam_type_from_image", lambda *a, **k: "joelho")
    monkeypatch.setattr(analyzer, "_build_content_parts", lambda *a, **k: ([], 0))
    monkeypatch.setattr(analyzer, "get_reference_images_as_bytes", lambda t: [])
    monkeypatch.setattr(analyzer, "_produce_final_analysis", lambda *a, **k: ("## 3. ACHADOS\n[FORTE] LCA", "m", []))
    locate = MagicMock(return_value=([{"numero": 1, "imagem": 1, "estrutura": "LCA", "local": "LCA",
                                       "certeza": "forte", "box_2d": [1, 1, 100, 100]}], None))
    monkeypatch.setattr(analyzer, "_locate_findings", locate)
    stages = []

    result = analyzer.analyze_exam_from_bytes([(b"img", "image/png")], "joelho.png", "chave", on_progress=stages.append)

    assert result["annotations"][0]["estrutura"] == "LCA"
    assert locate.call_args.args[4] == "## 3. ACHADOS\n[FORTE] LCA"  # localiza o laudo final
    assert "marcando_achados" in stages


# ── Persistência e exibição ───────────────────────────────────────────────────

def test_salvar_analise_com_imagens_grava_marcacoes(monkeypatch):
    salvar = MagicMock(return_value="laudo-1")
    monkeypatch.setattr(db, "salvar_analise", salvar)
    monkeypatch.setattr(db, "salvar_imagem_exame", MagicMock())
    result = {"exam_type": "joelho", "analysis": "x", "model_used": "m", "references_used": 0,
              "num_images": 1, "annotations": [{"numero": 1}]}

    db.salvar_analise_com_imagens(result, "premium", [(b"a", "image/png")], [{}])

    assert salvar.call_args.kwargs["marcacoes"] == [{"numero": 1}]


def test_result_view_revalida_marcacoes():
    result = {"analysis": "x", "exam_type": "joelho", "references_used": 0, "model_used": "m", "num_images": 1,
              "annotations": RESPOSTA["achados"] + [{"imagem": 1, "estrutura": "<script>", "certeza": "x"}]}
    view = presentation.result_view(result)
    assert [m["estrutura"] for m in view["annotations"]] == ["LCA", "Menisco medial"]  # imagem 2 não existe


def test_result_view_sem_marcacoes():
    view = presentation.result_view({"analysis": "x", "exam_type": "joelho", "references_used": 0, "model_used": "m"})
    assert view["annotations"] == []


def test_paginas_de_laudo_carregam_as_marcacoes():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("result.html", "laudo_detalhe.html", "trial.html"):
        html = (root / "templates" / name).read_text(encoding="utf-8")
        assert "laudo-marcacoes.js" in html and "initExamFindings(" in html
