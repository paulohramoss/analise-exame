#!/usr/bin/env python3
"""
Auditoria dos e-mails já cadastrados na tabela `clientes`.

Aplica a mesma validação do checkout (core/email_validation.py) aos registros
existentes e imprime um relatório do que hoje seria recusado.

SOMENTE LEITURA — nunca altera nem apaga nada no Supabase.

Uso:
    python scripts/auditar_emails.py                # relatório na tela
    python scripts/auditar_emails.py --sem-dns      # pula a checagem de MX
    python scripts/auditar_emails.py --csv out.csv  # salva os reprovados
    python scripts/auditar_emails.py --limite 5000
"""

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core import db  # noqa: E402
from core.email_validation import validar_email_cadastro  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Audita e-mails cadastrados (somente leitura).")
    parser.add_argument("--limite", type=int, default=1000, help="máximo de clientes a ler (padrão: 1000)")
    parser.add_argument("--sem-dns", action="store_true", help="não consultar MX (só sintaxe e descartáveis)")
    parser.add_argument("--csv", metavar="ARQUIVO", help="salva os reprovados em CSV")
    args = parser.parse_args()

    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_KEY"):
        print("ERRO: SUPABASE_URL / SUPABASE_SERVICE_KEY não configurados no .env.", file=sys.stderr)
        return 1

    clientes = db.listar_clientes(limite=args.limite)
    if not clientes:
        print("Nenhum cliente retornado (tabela vazia ou falha de conexão — ver logs).")
        return 1

    verificar_dns = None if not args.sem_dns else False
    reprovados = []

    for cliente in clientes:
        email_original = cliente.get("email") or ""
        _, erro = validar_email_cadastro(email_original, verificar_dns=verificar_dns)
        if erro:
            reprovados.append({
                "id": cliente.get("id", ""),
                "nome": cliente.get("nome", ""),
                "email": email_original,
                "created_at": cliente.get("created_at", ""),
                "motivo": erro,
            })

    total = len(clientes)
    print(f"\nClientes analisados: {total}")
    print(f"Reprovados na regra atual: {len(reprovados)} ({len(reprovados) * 100 // total if total else 0}%)")
    if args.sem_dns:
        print("(checagem de DNS/MX desativada nesta execução)")

    if reprovados:
        largura_email = max(len(r["email"]) for r in reprovados)
        largura_nome = max(len(r["nome"]) for r in reprovados)
        print()
        for r in reprovados:
            print(f"  {r['email']:<{largura_email}}  {r['nome']:<{largura_nome}}  {r['motivo']}")

    if args.csv and reprovados:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "nome", "email", "created_at", "motivo"])
            writer.writeheader()
            writer.writerows(reprovados)
        print(f"\nCSV salvo em {args.csv}")

    print("\nNada foi alterado no banco. Decida caso a caso o que fazer com os reprovados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
