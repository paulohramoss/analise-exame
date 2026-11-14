"""Testes da pontuação do conjunto de avaliação (evals/scoring.py) — sem chamar APIs."""

import json

import pytest
from pathlib import Path

from evals import scoring

CASE = {
    "id": "c1",
    "expected_region": "joelho",
    "expected_findings": [["rotura do lca", "ruptura do ligamento cruzado anterior"], ["derrame articular"]],
    "forbidden_findings": [["rotura do lcp"]],
}

GOOD_REPORT = """## 0. SINAIS DE ALERTA E ENCAMINHAMENTO
**Nenhum sinal de alerta identificado nas imagens analisadas.**
## 1. IDENTIFICAÇÃO DO EXAME
RM do joelho direito.
## 2. ANÁLISE ESTRUTURAL — COMPARAÇÃO COM PADRÃO NORMAL
Ligamentos avaliados.
## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
- **[FORTE]** Ruptura do ligamento cruzado anterior.
- **[MODERADO]** Derrame articular moderado.
> Achados descartados: rotura do LCP descartada.
## 4. IMPRESSÃO DE IMAGEM — HIPÓTESES
Rotura completa do LCA.
## 5. IMPACTO NA ESTRUTURA FÍSICA DO PACIENTE
Instabilidade.
## 6. CONDUTA, ENCAMINHAMENTO E REABILITAÇÃO
Avaliação ortopédica.
"""


def test_complete_report_passes():
    result = scoring.score_report(GOOD_REPORT, "joelho", CASE)
    assert result["score"] == 1.0
    assert result["passed"] is True
    assert result["missing_findings"] == []


def test_discarded_finding_outside_impression_is_not_penalized():
    """"Rotura do LCP" aparece só em "Achados descartados" (seção 3), não na impressão."""
    assert scoring.score_report(GOOD_REPORT, "joelho", CASE)["forbidden_in_impression"] == []


def test_forbidden_finding_in_impression_is_penalized():
    report = GOOD_REPORT.replace("Rotura completa do LCA.", "Rotura completa do LCA e rotura do LCP.")
    result = scoring.score_report(report, "joelho", CASE)
    assert result["forbidden_in_impression"] == ["rotura do lcp"]
    assert result["metrics"]["no_forbidden"] == 0.0


def test_missing_sections_wrong_region_and_findings_lower_the_score():
    report = "## 1. IDENTIFICAÇÃO DO EXAME\nRM do ombro.\n## 4. IMPRESSÃO DIAGNÓSTICA\nTendinopatia."
    result = scoring.score_report(report, "ombro", CASE)
    assert result["missing_sections"] == [0, 2, 3, 5, 6]
    assert result["metrics"]["region"] == 0.0
    assert result["metrics"]["findings_recall"] == 0.0
    assert result["passed"] is False


def test_compare_flags_regressions():
    baseline = {"cases": {"a": {"score": 0.9, "passed": True}, "b": {"score": 0.8, "passed": True}}}
    better = {"cases": {"a": {"score": 0.95, "passed": True}, "b": {"score": 0.8, "passed": True}}}
    worse = {"cases": {"a": {"score": 0.9, "passed": True}, "b": {"score": 0.6, "passed": False}}}

    assert scoring.compare(better, baseline)["regressed"] is False
    comparison = scoring.compare(worse, baseline)
    assert comparison["regressed"] is True
    assert comparison["newly_failing"] == ["b"]


def test_example_case_file_is_valid():
    case = json.loads((Path(__file__).parent.parent / "evals/cases/exemplo-joelho-lca.json").read_text(encoding="utf-8"))
    assert {"id", "images", "expected_region", "expected_findings"} <= set(case)
    assert all(isinstance(alternatives, list) and alternatives for alternatives in case["expected_findings"])


def test_old_section_titles_still_count():
    """Laudos salvos antes da mudança de títulos (seções 4 e 6) continuam pontuando."""
    old = GOOD_REPORT.replace("IMPRESSÃO DE IMAGEM — HIPÓTESES", "IMPRESSÃO DIAGNÓSTICA").replace(
        "CONDUTA, ENCAMINHAMENTO E REABILITAÇÃO", "CONDUTA E PRÓXIMOS PASSOS")
    assert scoring.score_report(old, "joelho", CASE)["missing_sections"] == []


FRACTURE_CASE = {**CASE, "expected_red_flags": [["fratura", "traco de fratura"]]}


def test_missed_red_flag_fails_case_even_with_high_score():
    """Falso negativo de sinal de alerta é o erro mais grave: reprova o caso."""
    result = scoring.score_report(GOOD_REPORT, "joelho", FRACTURE_CASE)
    assert result["missed_red_flags"] == ["fratura"]
    assert result["score"] >= 0.8
    assert result["passed"] is False


def test_red_flag_in_section_zero_passes():
    report = GOOD_REPORT.replace(
        "**Nenhum sinal de alerta identificado nas imagens analisadas.**",
        "- **[ALERTA]** Suspeita de fratura do platô tibial — **Encaminhar:** ortopedia (urgente)",
    )
    result = scoring.score_report(report, "joelho", FRACTURE_CASE)
    assert result["missed_red_flags"] == []
    assert result["metrics"]["red_flags"] == 1.0
    assert result["passed"] is True


def test_red_flag_mentioned_only_outside_section_zero_does_not_count():
    """Fratura citada na seção 3 mas ausente da seção 0 não chega ao destaque de alerta."""
    report = GOOD_REPORT.replace("Derrame articular moderado.", "Derrame articular e fratura do platô.")
    assert scoring.score_report(report, "joelho", FRACTURE_CASE)["missed_red_flags"] == ["fratura"]


@pytest.mark.parametrize("section_zero", [
    "**Nenhum sinal de alerta identificado nas imagens analisadas.** Sem evidência de fratura.",
    "Não há sinais de alerta. Fratura descartada.",
    "- Ausência de fratura ou lesão agressiva.",
])
def test_negated_red_flag_in_section_zero_is_still_missed(section_zero):
    """ "Sem evidência de fratura" na seção 0 nega o alerta: não pode contar como detectado."""
    report = GOOD_REPORT.replace(
        "**Nenhum sinal de alerta identificado nas imagens analisadas.**", section_zero,
    ).replace("Derrame articular moderado.", "Fratura do platô tibial.")
    result = scoring.score_report(report, "joelho", FRACTURE_CASE)
    assert result["missed_red_flags"] == ["fratura"]
    assert result["passed"] is False


def test_red_flag_without_tag_but_affirmative_counts():
    report = GOOD_REPORT.replace(
        "**Nenhum sinal de alerta identificado nas imagens analisadas.**",
        "- Suspeita de fratura do platô tibial — encaminhar à ortopedia (urgente)",
    )
    assert scoring.score_report(report, "joelho", FRACTURE_CASE)["missed_red_flags"] == []


def test_false_alarm_is_recorded_but_not_penalized():
    normal_case = {**CASE, "expected_red_flags": []}
    report = GOOD_REPORT.replace(
        "**Nenhum sinal de alerta identificado nas imagens analisadas.**",
        "- **[ALERTA]** Suspeita de lesão óssea — **Encaminhar:** ortopedia (prioritário)",
    )
    result = scoring.score_report(report, "joelho", normal_case)
    assert result["false_alarm"] is True
    assert result["score"] == 1.0
    assert scoring.score_report(GOOD_REPORT, "joelho", normal_case)["false_alarm"] is False


def test_all_case_files_are_valid():
    for path in (Path(__file__).parent.parent / "evals/cases").glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        assert {"id", "images", "expected_region", "expected_findings"} <= set(case), path.name
        for key in ("expected_findings", "forbidden_findings", "expected_red_flags"):
            assert all(isinstance(alts, list) and alts for alts in case.get(key, [])), (path.name, key)
