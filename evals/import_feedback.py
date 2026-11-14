#!/usr/bin/env python3
"""
Transforma os erros apontados pelos clientes ("A IA errou isso") em rascunhos de caso.

Uso:
    python -m evals.import_feedback                  # últimos 30 dias
    python -m evals.import_feedback --since 2026-11-01

Lê feedbacks_ia (secao = 'erro_ia', ou o formato antigo "[erro_ia:...]" no
comentário) no Supabase do .env e:

- imprime quantos erros houve por categoria e por região — o placar do que a
  IA mais erra, com ou sem autorização;
- para cada laudo cujo profissional AUTORIZOU o uso, grava (ou atualiza)
  evals/cases/rascunhos/<id>.json com o laudo gerado, a descrição clínica (já
  sem identificação) e os apontamentos.

Os rascunhos não rodam: load_cases lê só evals/cases/*.json. Para virar caso,
alguém da equipe precisa (1) obter as imagens anonimizadas — o app não guarda
imagens de exame —, (2) preencher expected_findings/expected_red_flags com o
padrão-ouro de um especialista e (3) mover o arquivo para evals/cases/.
A pasta de rascunhos está no .gitignore: ela contém texto clínico.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

DRAFTS_DIR = EVALS_DIR / "cases" / "rascunhos"
_LEGACY_PREFIX = "[erro_ia:"  # core.db.ERRO_IA_PREFIXO
_MAX_REPORT_CHARS = 20000

# O que cada categoria sugere para o padrão-ouro do caso.
_HINTS = {
    "achado_inventado": "Achado apontado pela IA não existe: inclua-o em forbidden_findings.",
    "achado_perdido": "A IA não viu um achado: inclua-o em expected_findings.",
    "alerta_perdido": "A IA não deu um sinal de alerta: inclua-o em expected_red_flags (caso reprova sem ele).",
    "alerta_indevido": "Alerta sem motivo: use \"expected_red_flags\": [] se o exame não tiver sinal de alerta.",
    "regiao_lado": "Região ou lado errado: confira expected_region e a lateralidade na descrição.",
    "classificacao": "Grau/classificação errado: inclua o grau correto em expected_findings (com sinônimos).",
}


def parse_feedback(row: dict) -> dict | None:
    """{categoria, comentario, autoriza} de uma linha de feedbacks_ia; None se não for erro apontado."""
    if row.get("secao") == "erro_ia" and row.get("categoria_erro"):
        return {
            "categoria": row["categoria_erro"],
            "comentario": row.get("comentario") or "",
            "autoriza": bool(row.get("autoriza_avaliacao")),
        }
    comentario = row.get("comentario") or ""
    if comentario.startswith(_LEGACY_PREFIX) and "]" in comentario:
        categoria, resto = comentario[len(_LEGACY_PREFIX):].split("]", 1)
        # Formato antigo não guarda autorização: conta no placar, não vira rascunho.
        return {"categoria": categoria, "comentario": resto.strip(), "autoriza": False}
    return None


def build_draft(analise: dict, apontamentos: list[dict], existing: dict | None = None) -> dict:
    """Rascunho de caso no formato de evals/scoring.py, preservando o que a equipe já preencheu."""
    draft = existing or {
        "id": f"rascunho-{analise['id'][:8]}",
        "images": [f"images/{analise['id'][:8]}-{n}.jpg" for n in range(1, (analise.get("num_imagens") or 1) + 1)],
        "description": analise.get("descricao_usuario") or "",
        "expected_region": analise.get("tipo_exame") or "",
        "expected_findings": [],
        "forbidden_findings": [],
        "pass_score": 0.7,
    }
    # Uma janela --since nova não pode apagar apontamentos de execuções anteriores.
    anteriores = (draft.get("rascunho") or {}).get("apontamentos") or []
    vistos, todos = set(), []
    for a in anteriores + [{"categoria": a["categoria"], "comentario": a["comentario"]} for a in apontamentos]:
        chave = (a["categoria"], a["comentario"])
        if chave not in vistos:
            vistos.add(chave)
            todos.append(a)
    categorias = sorted({a["categoria"] for a in todos})
    draft["rascunho"] = {
        "status": "PENDENTE: obter imagens anonimizadas e padrão-ouro de especialista",
        "analise_id": analise["id"],
        "criado_em": str(analise.get("created_at") or "")[:10],
        "modalidade": analise.get("modalidade"),
        "categorias": categorias,
        "o_que_preencher": [_HINTS[c] for c in categorias if c in _HINTS],
        "apontamentos": todos,
        "laudo_ia": (analise.get("analise_completa") or "")[:_MAX_REPORT_CHARS],
    }
    return draft


def _fetch(client, since: str) -> tuple[list[dict], dict[str, dict]]:
    rows = (
        client.table("feedbacks_ia").select("*")
        .gte("created_at", since).order("created_at").execute().data or []
    )
    ids = sorted({r["analise_id"] for r in rows if r.get("analise_id") and parse_feedback(r)})
    analises = {}
    for i in range(0, len(ids), 100):
        batch = (
            client.table("analises")
            .select("id, created_at, tipo_exame, modalidade, num_imagens, descricao_usuario, analise_completa")
            .in_("id", ids[i:i + 100]).execute().data or []
        )
        analises.update({a["id"]: a for a in batch})
    return rows, analises


def main() -> int:
    parser = argparse.ArgumentParser(description="Erros apontados pelos clientes → rascunhos de caso.")
    parser.add_argument("--since", default=str(date.today() - timedelta(days=30)), help="data inicial (AAAA-MM-DD)")
    args = parser.parse_args()

    from core import db

    client = db._get_client()
    if client is None:
        print("Supabase não configurado (SUPABASE_URL / chave no .env).", file=sys.stderr)
        return 1

    rows, analises = _fetch(client, args.since)
    por_analise: dict[str, list[dict]] = {}
    categorias, regioes = Counter(), Counter()
    for row in rows:
        apontamento = parse_feedback(row)
        if not apontamento or row.get("analise_id") not in analises:
            continue
        categorias[apontamento["categoria"]] += 1
        regioes[analises[row["analise_id"]].get("tipo_exame") or "?"] += 1
        por_analise.setdefault(row["analise_id"], []).append(apontamento)

    print(f"Erros apontados desde {args.since}: {sum(categorias.values())} em {len(por_analise)} laudos")
    for categoria, total in categorias.most_common():
        print(f"  {categoria:<18} {total}")
    if regioes:
        print("Por região: " + ", ".join(f"{r} {n}" for r, n in regioes.most_common()))

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    gravados = 0
    for analise_id, apontamentos in por_analise.items():
        autorizados = [a for a in apontamentos if a["autoriza"]]
        if not autorizados:
            continue
        path = DRAFTS_DIR / f"{analise_id[:8]}.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        draft = build_draft(analises[analise_id], autorizados, existing)
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        gravados += 1
    print(f"Rascunhos gravados/atualizados em {DRAFTS_DIR.relative_to(EVALS_DIR.parent)}: {gravados}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
