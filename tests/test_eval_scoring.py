"""Testes da pontuação do conjunto de avaliação (evals/scoring.py) — sem chamar APIs."""

import json
from pathlib import Path

from evals import scoring

CASE = {
    "id": "c1",
    "expected_region": "joelho",
    "expected_findings": [["rotura do lca", "ruptura do ligamento cruzado anterior"], ["derrame articular"]],
    "forbidden_findings": [["rotura do lcp"]],
}

GOOD_REPORT = """## 1. IDENTIFICAÇÃO DO EXAME
RM do joelho direito.
## 2. ANÁLISE ESTRUTURAL — COMPARAÇÃO COM PADRÃO NORMAL
Ligamentos avaliados.
## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
- **[FORTE]** Ruptura do ligamento cruzado anterior.
- **[MODERADO]** Derrame articular moderado.
> Achados descartados: rotura do LCP descartada.
## 4. IMPRESSÃO DIAGNÓSTICA
Rotura completa do LCA.
## 5. IMPACTO NA ESTRUTURA FÍSICA DO PACIENTE
Instabilidade.
## 6. CONDUTA E PRÓXIMOS PASSOS
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
    assert result["missing_sections"] == [2, 3, 5, 6]
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
