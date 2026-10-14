#!/usr/bin/env python3
"""
Roda o conjunto de casos de referência e pontua os laudos (ver evals/scoring.py).

Uso:
    python -m evals.run_eval                           # roda todos os casos (CHAMA AS APIs — custa)
    python -m evals.run_eval --only joelho-lca-01      # um caso
    python -m evals.run_eval --baseline evals/results/2026-10-01.json
    python -m evals.run_eval --rescore evals/results/2026-10-01.json   # sem API: repontua laudos salvos

Saída: evals/results/<data-hora>.json com o laudo, a pontuação e a configuração
(modelos, orçamento) de cada caso. Com --baseline, sai com código 1 se houver
regressão — dá para usar antes de trocar prompt ou modelo.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from evals import scoring  # noqa: E402

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def load_cases(cases_dir: Path, only: str | None = None) -> list[dict]:
    cases = []
    for path in sorted(cases_dir.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if only and case["id"] != only:
            continue
        cases.append(case)
    return cases


def run_case(case: dict) -> dict:
    from core import analyzer

    images = []
    for rel in case["images"]:
        path = EVALS_DIR / rel
        images.append((path.read_bytes(), _MIME.get(path.suffix.lower(), "image/jpeg")))

    t0 = time.time()
    result = analyzer.analyze_exam_from_bytes(
        exam_images_data=images,
        exam_filename=Path(case["images"][0]).name,
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
        user_description=case.get("description", ""),
        model_name=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )
    return {
        "report": result["analysis"],
        "exam_type": result["exam_type"],
        "model_used": result["model_used"],
        "seconds": round(time.time() - t0, 1),
        "usage": result.get("usage", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Avaliação de qualidade dos laudos.")
    parser.add_argument("--cases", type=Path, default=EVALS_DIR / "cases")
    parser.add_argument("--only", help="id de um caso")
    parser.add_argument("--rescore", type=Path, help="repontua laudos de uma execução salva, sem chamar APIs")
    parser.add_argument("--baseline", type=Path, help="execução anterior para comparar")
    parser.add_argument("--max-drop", type=float, default=0.05, help="queda máxima tolerada na média")
    parser.add_argument("--out", type=Path, help="arquivo de saída (padrão: evals/results/<data-hora>.json)")
    args = parser.parse_args()

    cases = {case["id"]: case for case in load_cases(args.cases, args.only)}
    if not cases:
        print(f"Nenhum caso em {args.cases}.", file=sys.stderr)
        return 1

    previous = json.loads(args.rescore.read_text(encoding="utf-8")) if args.rescore else None
    run = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            "claude_model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            "analysis_budget_seconds": os.environ.get("ANALYSIS_BUDGET_SECONDS", "90"),
            "rescored_from": str(args.rescore) if args.rescore else None,
        },
        "cases": {},
    }

    for case_id, case in cases.items():
        if previous:
            if case_id not in previous["cases"]:
                continue
            output = {k: previous["cases"][case_id][k] for k in ("report", "exam_type", "model_used")}
        else:
            missing = [rel for rel in case["images"] if not (EVALS_DIR / rel).exists()]
            if missing:
                print(f"[pulado] {case_id}: imagem ausente ({', '.join(missing)})")
                continue
            print(f"[rodando] {case_id}…", flush=True)
            output = run_case(case)
        score = scoring.score_report(output["report"], output["exam_type"], case)
        run["cases"][case_id] = {**output, **score}
        status = "ok " if score["passed"] else "FALHOU"
        print(f"[{status}] {case_id}: {score['score']:.2f}  faltando={score['missing_findings']}  "
              f"proibidos={score['forbidden_in_impression']}")

    if not run["cases"]:
        print("Nenhum caso executado.", file=sys.stderr)
        return 1

    scores = [c["score"] for c in run["cases"].values()]
    run["summary"] = {
        "cases": len(scores),
        "mean_score": round(sum(scores) / len(scores), 3),
        "passed": sum(1 for c in run["cases"].values() if c["passed"]),
    }
    print(f"\nMédia {run['summary']['mean_score']:.3f} — {run['summary']['passed']}/{len(scores)} casos aprovados")

    exit_code = 0
    if args.baseline:
        comparison = scoring.compare(run, json.loads(args.baseline.read_text(encoding="utf-8")), args.max_drop)
        run["comparison"] = {"baseline": str(args.baseline), **comparison}
        print(f"Comparado a {args.baseline.name}: Δ média {comparison['mean_delta']:+.3f}, "
              f"passaram a falhar: {comparison['newly_failing'] or 'nenhum'}")
        if comparison["regressed"]:
            print("REGRESSÃO detectada.")
            exit_code = 1

    out = args.out or EVALS_DIR / "results" / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Resultado salvo em {out}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
