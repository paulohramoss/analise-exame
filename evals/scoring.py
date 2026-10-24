"""
Pontuação objetiva de um laudo gerado contra um caso de referência.

Não substitui a revisão de um especialista: mede o que dá para medir sem
ele — estrutura do laudo, região detectada, achados esperados presentes e
achados que NÃO deveriam estar na impressão diagnóstica. Serve para comparar
duas versões (prompt, modelo, orçamento de tempo) no mesmo conjunto de casos.

Formato do caso (evals/cases/*.json):
    {
      "id": "joelho-lca-01",
      "images": ["images/joelho-lca-01.jpg"],        # relativo a evals/
      "description": "RM joelho direito, 28 anos, trauma em pivô",
      "expected_region": "joelho",
      "expected_findings": [                          # cada item: sinônimos aceitos
        ["rotura do lca", "ruptura do ligamento cruzado anterior"],
        ["edema osseo", "contusao ossea"]
      ],
      "forbidden_findings": [["rotura do menisco medial"]],   # não pode na impressão
      "pass_score": 0.7                               # opcional (padrão 0.7)
    }
"""

import re
import unicodedata

REQUIRED_SECTIONS = {
    1: "identificacao",
    2: "analise estrutural",
    3: "achados",
    4: "impressao diagnostica",
    5: "impacto",
    6: "conduta",
}
DEFAULT_PASS_SCORE = 0.7
WEIGHTS = {"findings_recall": 0.4, "sections": 0.2, "region": 0.2, "evidence_labels": 0.1, "no_forbidden": 0.1}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9\[\]]+", " ", plain).split())


_HEADING = re.compile(r"^\s*#{1,4}\s*(\d)\s*[.)]?\s*(.*)$", re.MULTILINE)


def split_sections(report: str) -> dict[int, str]:
    """{número da seção: texto} a partir dos títulos "## N. TÍTULO" do laudo."""
    matches = list(_HEADING.finditer(report or ""))
    sections = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(report)
        number = int(match.group(1))
        if number in REQUIRED_SECTIONS and normalize(match.group(2)).startswith(REQUIRED_SECTIONS[number].split()[0]):
            sections[number] = report[match.end():end]
    return sections


def _mentions(text_norm: str, alternatives: list[str]) -> bool:
    return any(f" {normalize(alt)} " in f" {text_norm} " for alt in alternatives)


def score_report(report: str, exam_type: str, case: dict) -> dict:
    # A seção de referências do PubMed (core/pubmed.py) lista títulos de artigos:
    # um achado citado ali não pode contar como achado do laudo.
    report = re.split(r"(?m)^##\s*7\.\s*REFER", report)[0]
    sections = split_sections(report)
    report_norm = normalize(report)
    impression_norm = normalize(sections.get(4, ""))

    expected = case.get("expected_findings") or []
    found = [alts[0] for alts in expected if _mentions(report_norm, alts)]
    missing = [alts[0] for alts in expected if alts[0] not in found]
    forbidden = [alts[0] for alts in case.get("forbidden_findings") or [] if _mentions(impression_norm, alts)]

    metrics = {
        "findings_recall": len(found) / len(expected) if expected else 1.0,
        "sections": len(sections) / len(REQUIRED_SECTIONS),
        "region": 1.0 if not case.get("expected_region") or exam_type == case["expected_region"] else 0.0,
        "evidence_labels": 1.0 if ("[FORTE]" in report or "[MODERADO]" in report) else 0.0,
        "no_forbidden": 0.0 if forbidden else 1.0,
    }
    score = sum(WEIGHTS[name] * value for name, value in metrics.items())
    return {
        "score": round(score, 3),
        "passed": score >= case.get("pass_score", DEFAULT_PASS_SCORE),
        "metrics": {name: round(value, 3) for name, value in metrics.items()},
        "missing_sections": [n for n in REQUIRED_SECTIONS if n not in sections],
        "found_findings": found,
        "missing_findings": missing,
        "forbidden_in_impression": forbidden,
        "weak_label_present": "[FRACO]" in report,
        "words": len(report_norm.split()),
    }


def compare(current: dict, baseline: dict, max_mean_drop: float = 0.05) -> dict:
    """Compara duas execuções ({"cases": {id: {"score", "passed"}}}). Regressão = queda da média
    acima de max_mean_drop ou caso que passava e agora falha."""
    shared = sorted(set(current["cases"]) & set(baseline["cases"]))
    if not shared:
        return {"shared_cases": 0, "mean_delta": 0.0, "newly_failing": [], "regressed": False}
    mean = lambda run: sum(run["cases"][cid]["score"] for cid in shared) / len(shared)  # noqa: E731
    delta = mean(current) - mean(baseline)
    newly_failing = [cid for cid in shared if baseline["cases"][cid]["passed"] and not current["cases"][cid]["passed"]]
    return {
        "shared_cases": len(shared),
        "mean_delta": round(delta, 3),
        "newly_failing": newly_failing,
        "regressed": delta < -max_mean_drop or bool(newly_failing),
    }
