"""
Chave do prontuário (PRONTUARIO_ENCRYPTION_KEY): gerar, conferir, verificar e rotacionar.

Se a chave se perde, nomes e CPFs dos pacientes ficam ilegíveis para sempre.
Este script existe para que isso nunca aconteça em silêncio:

    python -m scripts.prontuario_chaves gerar
        Nova chave Fernet + impressão digital. Guarde a chave no cofre de
        senhas (fora da Vercel) e anote a impressão digital.

    python -m scripts.prontuario_chaves impressao
        Impressão digital da chave configurada no ambiente. Compare com a
        anotada no backup: iguais = o backup é a chave em uso.

    python -m scripts.prontuario_chaves verificar
        Tenta abrir todos os prontuários. Diz quantos abrem com a chave atual,
        quantos só com uma antiga (precisam de rotação) e quantos com nenhuma
        (perdidos — PARE e recupere a chave antes de qualquer outra troca).
        Sai com código 1 se houver ilegíveis (serve para CI/cron).

    python -m scripts.prontuario_chaves rotacionar [--aplicar]
        Roteiro de troca de chave:
          1. `gerar` a nova; guardar no cofre.
          2. Na Vercel: PRONTUARIO_ENCRYPTION_KEY=<nova> e
             PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS=<antiga>. Deploy.
          3. `rotacionar` (simulação) e depois `rotacionar --aplicar`.
          4. `verificar` deve mostrar 0 em "só chave antiga" e 0 ilegíveis.
          5. Só então remover PRONTUARIO_ENCRYPTION_KEYS_ANTIGAS.
"""

import argparse
import sys

from cryptography.fernet import Fernet

from core import db, patient_crypto

_PAGE = 500


def _linhas(client):
    inicio = 0
    while True:
        res = (
            client.table("pacientes").select("id, nome_cifrado, cpf_cifrado")
            .order("id").range(inicio, inicio + _PAGE - 1).execute()
        )
        linhas = res.data or []
        yield from linhas
        if len(linhas) < _PAGE:
            return
        inicio += _PAGE


def _abre(token: str | None) -> str:
    """'atual', 'antiga' ou 'nenhuma'."""
    if not token:
        return "atual"  # campo vazio não tem o que abrir
    if patient_crypto.opens_with_primary(token):
        return "atual"
    return "antiga" if patient_crypto.decrypt(token) else "nenhuma"


def verificar(client) -> dict:
    stats = {"total": 0, "atual": 0, "antiga": 0, "nenhuma": 0, "ilegiveis": []}
    for linha in _linhas(client):
        stats["total"] += 1
        estados = {_abre(linha.get("nome_cifrado")), _abre(linha.get("cpf_cifrado"))}
        estado = "nenhuma" if "nenhuma" in estados else "antiga" if "antiga" in estados else "atual"
        stats[estado] += 1
        if estado == "nenhuma":
            stats["ilegiveis"].append(linha["id"])
    return stats


def rotacionar(client, aplicar: bool = False) -> dict:
    stats = {"total": 0, "recifrados": 0, "ja_atuais": 0, "falhas": []}
    for linha in _linhas(client):
        stats["total"] += 1
        nome, cpf = linha.get("nome_cifrado"), linha.get("cpf_cifrado")
        if all(patient_crypto.opens_with_primary(t) for t in (nome, cpf) if t):
            stats["ja_atuais"] += 1
            continue
        try:
            novo_nome = patient_crypto.rotate(nome) if nome else nome
            novo_cpf = patient_crypto.rotate(cpf) if cpf else cpf
            cpf_claro = patient_crypto.decrypt(novo_cpf) if novo_cpf else ""
        except Exception:
            stats["falhas"].append(linha["id"])
            continue
        if aplicar:
            update = {"nome_cifrado": novo_nome, "cpf_cifrado": novo_cpf}
            if cpf_claro:
                update["cpf_hash"] = patient_crypto.cpf_hash(cpf_claro)
            client.table("pacientes").update(update).eq("id", linha["id"]).execute()
        stats["recifrados"] += 1
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gerar")
    sub.add_parser("impressao")
    sub.add_parser("verificar")
    rot = sub.add_parser("rotacionar")
    rot.add_argument("--aplicar", action="store_true", help="grava no banco (sem isso, só simula)")
    args = parser.parse_args(argv)

    if args.cmd == "gerar":
        key = Fernet.generate_key().decode()
        print(f"Chave: {key}")
        print(f"Impressão digital: {patient_crypto.fingerprint(key)}")
        print("Guarde a chave no cofre de senhas ANTES de configurá-la no ambiente.")
        return 0

    if not patient_crypto.is_configured():
        print("PRONTUARIO_ENCRYPTION_KEY não configurada ou inválida.", file=sys.stderr)
        return 2

    if args.cmd == "impressao":
        print(patient_crypto.fingerprint())
        return 0

    client = db._get_client()
    if client is None:
        print("Supabase não configurado (SUPABASE_URL / SUPABASE_SERVICE_KEY).", file=sys.stderr)
        return 2

    if args.cmd == "verificar":
        s = verificar(client)
        print(f"Pacientes: {s['total']} | chave atual: {s['atual']} | só chave antiga: {s['antiga']} "
              f"| ilegíveis: {s['nenhuma']}")
        if s["ilegiveis"]:
            print("ILEGÍVEIS (ids): " + ", ".join(s["ilegiveis"][:50]), file=sys.stderr)
            return 1
        return 0

    s = rotacionar(client, aplicar=args.aplicar)
    modo = "gravados" if args.aplicar else "a recifrar (simulação; use --aplicar)"
    print(f"Pacientes: {s['total']} | já na chave atual: {s['ja_atuais']} | {modo}: {s['recifrados']} "
          f"| falhas: {len(s['falhas'])}")
    return 1 if s["falhas"] else 0


if __name__ == "__main__":
    sys.exit(main())
