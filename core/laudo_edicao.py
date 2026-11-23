"""
Edição do laudo pelo profissional: o laudo passa a ser dele, com apoio da IA.

Na primeira edição, o texto gerado pela IA é copiado para
`analises.analise_ia` e nunca mais muda; `analise_completa` vira a versão do
profissional (é ela que o PDF, o relatório ao paciente e a correlação usam).
Cada edição grava um resumo do que mudou (`edicao_resumo`), por seção do laudo:
é o dado que mostra quais achados a IA mais erra e quanto do texto o
profissional confirma ("X% do laudo mantido").

O resumo não guarda o texto do laudo — só contagens e títulos de seção —, então
pode ser agregado no painel sem expor conteúdo clínico.
"""

import difflib
import re

MAX_CHARS = 60_000

_SECTION_RE = re.compile(r"^\s*#{2,3}\s+(.+?)\s*$")
_ACHADO_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def normalizar(texto: str) -> str:
    """Quebras de linha uniformes e sem espaço sobrando no fim (o textarea manda \\r\\n)."""
    linhas = (texto or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(linha.rstrip() for linha in linhas).strip()


def _secoes(texto: str) -> dict[str, list[str]]:
    """Título da seção → linhas não vazias. O que vem antes do 1º título fica em ''."""
    secoes: dict[str, list[str]] = {"": []}
    atual = ""
    for linha in normalizar(texto).split("\n"):
        m = _SECTION_RE.match(linha)
        if m:
            atual = m.group(1).strip()[:120]
            secoes.setdefault(atual, [])
            continue
        if linha.strip():
            secoes[atual].append(linha.strip())
    return secoes


def _achados(linhas: list[str]) -> list[str]:
    return [_ACHADO_RE.sub("", linha) for linha in linhas if _ACHADO_RE.match(linha)]


def resumir(texto_ia: str, texto_profissional: str) -> dict:
    """O que o profissional mudou em relação à IA, sem copiar o texto."""
    ia, prof = _secoes(texto_ia), _secoes(texto_profissional)
    linhas_ia = [linha for linhas in ia.values() for linha in linhas]
    linhas_prof = [linha for linhas in prof.values() for linha in linhas]
    matcher = difflib.SequenceMatcher(a=linhas_ia, b=linhas_prof, autojunk=False)
    mantidas = sum(bloco.size for bloco in matcher.get_matching_blocks())

    secoes = []
    for titulo in list(dict.fromkeys([*ia, *prof])):
        antes, depois = ia.get(titulo, []), prof.get(titulo, [])
        if antes == depois:
            continue
        achados_antes, achados_depois = set(_achados(antes)), set(_achados(depois))
        secoes.append({
            "secao": titulo or "(início)",
            "removida": titulo in ia and titulo not in prof,
            "nova": titulo in prof and titulo not in ia,
            "achados_removidos": len(achados_antes - achados_depois),
            "achados_adicionados": len(achados_depois - achados_antes),
        })

    return {
        "linhas_ia": len(linhas_ia),
        "linhas_mantidas": mantidas,
        "linhas_removidas": len(linhas_ia) - mantidas,
        "linhas_adicionadas": len(linhas_prof) - mantidas,
        "percentual_mantido": round(100 * mantidas / len(linhas_ia)) if linhas_ia else 100,
        "secoes": secoes,
    }


def agregar(resumos: list[dict], total_laudos: int) -> dict:
    """Métricas para o painel: quanto da IA é confirmado e onde ela mais é corrigida."""
    editados = [r for r in resumos if isinstance(r, dict)]
    por_secao: dict[str, dict] = {}
    for resumo in editados:
        for secao in resumo.get("secoes") or []:
            nome = re.sub(r"^\d+\.\s*", "", str(secao.get("secao") or ""))[:80] or "(início)"
            item = por_secao.setdefault(nome, {"secao": nome, "laudos": 0, "removidos": 0, "adicionados": 0})
            item["laudos"] += 1
            item["removidos"] += int(secao.get("achados_removidos") or 0)
            item["adicionados"] += int(secao.get("achados_adicionados") or 0)

    linhas_ia = sum(int(r.get("linhas_ia") or 0) for r in editados)
    mantidas = sum(int(r.get("linhas_mantidas") or 0) for r in editados)
    nao_editados = max(total_laudos - len(editados), 0)
    # Laudo não editado = 100% confirmado; nos editados conta a proporção de linhas mantidas.
    confirmados = (nao_editados * 100 + sum(int(r.get("percentual_mantido") or 0) for r in editados))
    return {
        "laudos": total_laudos,
        "editados": len(editados),
        "percentual_editados": round(100 * len(editados) / total_laudos) if total_laudos else 0,
        "percentual_mantido_nos_editados": round(100 * mantidas / linhas_ia) if linhas_ia else 100,
        "percentual_confirmado": round(confirmados / total_laudos) if total_laudos else 0,
        "secoes_mais_corrigidas": sorted(por_secao.values(), key=lambda s: (-s["laudos"], s["secao"]))[:6],
    }
