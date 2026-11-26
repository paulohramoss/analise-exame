"""
Retorno do paciente: o que mudou entre dois exames do mesmo tipo.

Sem IA: compara os valores já lidos e calculados (`analises.dados_estruturados`)
de dois laudos e diz, item a item, se melhorou, piorou ou ficou estável.

- Laboratoriais: casa os exames pelo nome; "melhorou" = voltou para a faixa
  de referência ou se aproximou dela; "piorou" = saiu da faixa ou se afastou.
  Dentro da faixa nos dois exames, a mudança é só registrada (neutra).
- Dinamometria: casa grupo muscular + ação + velocidade/ângulo e compara o
  LSI do pico de torque (maior = melhor; ≥ 90% é o critério de retorno);
  plataforma de força pela assimetria (menor = melhor).
- Imagem e correlação: não há valores; mostra as seções de impressão e
  achados dos dois laudos lado a lado.
"""

import re
import unicodedata

ESTAVEL_PCT = 3.0  # variação menor que isso (relativa) não muda a direção


def _chave(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).strip()


def _num(valor) -> str:
    if valor is None:
        return "—"
    if isinstance(valor, float) and not valor.is_integer():
        return f"{valor:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(int(valor)) if isinstance(valor, float) else str(valor)


def _variacao(antes: float | None, depois: float | None) -> tuple[float | None, float | None]:
    if antes is None or depois is None:
        return None, None
    delta = round(depois - antes, 2)
    pct = round(delta / abs(antes) * 100, 1) if antes else None
    return delta, pct


def _distancia_da_faixa(r: dict) -> float | None:
    valor, minimo, maximo = r.get("valor"), r.get("referencia_min"), r.get("referencia_max")
    if valor is None:
        return None
    if minimo is not None and valor < minimo:
        return (minimo - valor) / (abs(minimo) or 1)
    if maximo is not None and valor > maximo:
        return (valor - maximo) / (abs(maximo) or 1)
    return 0.0


def _direcao_lab(antes: dict, depois: dict, pct: float | None) -> str:
    fora = {"abaixo", "acima"}
    sa, sd = antes.get("situacao"), depois.get("situacao")
    if sa in fora and sd == "normal":
        return "melhorou"
    if sa == "normal" and sd in fora:
        return "piorou"
    if sa in fora and sd in fora:
        if sa != sd:
            return "piorou"  # cruzou a faixa (ex.: de baixo para alto)
        da, dd = _distancia_da_faixa(antes), _distancia_da_faixa(depois)
        if da is not None and dd is not None and pct is not None and abs(pct) >= ESTAVEL_PCT:
            return "melhorou" if dd < da else "piorou" if dd > da else "estavel"
        return "estavel"
    if pct is None or abs(pct) < ESTAVEL_PCT:
        return "estavel"
    return "neutra"


def comparar_laboratorial(antes: dict, depois: dict) -> list[dict]:
    anteriores = {_chave(r["exame"]): r for r in antes.get("resultados") or [] if r.get("exame")}
    linhas, vistos = [], set()
    for r in depois.get("resultados") or []:
        chave = _chave(r.get("exame", ""))
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        a = anteriores.get(chave)
        un = r.get("unidade") or ""
        if a is None:
            linhas.append(_linha(r["exame"], None, r.get("valor"), un, "nova", r.get("painel"), depois_sit=r.get("situacao")))
            continue
        if (a.get("unidade") or "").lower() != un.lower() and a.get("unidade") and un:
            linhas.append(_linha(r["exame"], a.get("valor"), r.get("valor"), un, "unidade_diferente", r.get("painel"),
                                 antes_sit=a.get("situacao"), depois_sit=r.get("situacao"), unidade_antes=a.get("unidade")))
            continue
        _, pct = _variacao(a.get("valor"), r.get("valor"))
        linhas.append(_linha(r["exame"], a.get("valor"), r.get("valor"), un, _direcao_lab(a, r, pct), r.get("painel"),
                             antes_sit=a.get("situacao"), depois_sit=r.get("situacao")))
    for chave, a in anteriores.items():
        if chave not in vistos:
            linhas.append(_linha(a["exame"], a.get("valor"), None, a.get("unidade") or "", "ausente", a.get("painel"),
                                 antes_sit=a.get("situacao")))
    return linhas


def _linha(item, antes, depois, unidade, direcao, grupo=None, antes_sit=None, depois_sit=None, unidade_antes=None):
    delta, pct = _variacao(antes, depois) if direcao != "unidade_diferente" else (None, None)
    un = f" {unidade}" if unidade else ""
    un_antes = f" {unidade_antes}" if unidade_antes else un
    return {
        "item": item,
        "grupo": grupo or "",
        "antes": antes,
        "depois": depois,
        "antes_fmt": f"{_num(antes)}{un_antes}" if antes is not None else "—",
        "depois_fmt": f"{_num(depois)}{un}" if depois is not None else "—",
        "delta": delta,
        "delta_fmt": (("+" if delta > 0 else "") + _num(delta) + un) if delta else "",
        "delta_pct": pct,
        "direcao": direcao,
        "antes_situacao": antes_sit,
        "depois_situacao": depois_sit,
    }


def _medida_chave(m: dict) -> str:
    return "|".join([
        _chave(m.get("grupo_muscular", "")), m.get("acao") or "",
        str(m.get("velocidade_graus_s") or ""), str(m.get("angulo_graus") or ""),
    ])


def _medida_nome(m: dict) -> str:
    partes = [m.get("grupo_muscular") or "Medida"]
    if m.get("acao"):
        partes.append(m["acao"])
    if m.get("velocidade_graus_s"):
        partes.append(f"{_num(m['velocidade_graus_s'])}°/s")
    if m.get("angulo_graus"):
        partes.append(f"{_num(m['angulo_graus'])}°")
    return " · ".join(partes)


def _direcao_maior_melhor(antes, depois, limiar_abs: float = 2.0) -> str:
    if antes is None or depois is None:
        return "estavel"
    if abs(depois - antes) < limiar_abs:
        return "estavel"
    return "melhorou" if depois > antes else "piorou"


def comparar_dinamometria(antes: dict, depois: dict) -> list[dict]:
    anteriores = {_medida_chave(m): m for m in antes.get("medidas") or []}
    linhas, vistos = [], set()
    for m in depois.get("medidas") or []:
        chave = _medida_chave(m)
        vistos.add(chave)
        lsi_depois = (m.get("pico") or {}).get("lsi_pct")
        a = anteriores.get(chave)
        if a is None:
            linhas.append(_linha(f"LSI {_medida_nome(m)}", None, lsi_depois, "%", "nova", "Simetria"))
            continue
        lsi_antes = (a.get("pico") or {}).get("lsi_pct")
        linha = _linha(f"LSI {_medida_nome(m)}", lsi_antes, lsi_depois, "%",
                       _direcao_maior_melhor(lsi_antes, lsi_depois), "Simetria")
        linha["criterio_retorno"] = (m.get("pico") or {}).get("criterio_retorno")
        linhas.append(linha)
        for lado in ("direito", "esquerdo"):
            va, vd = a.get(f"pico_{lado}"), m.get(f"pico_{lado}")
            if va is not None and vd is not None:
                _, pct = _variacao(va, vd)
                direcao = "estavel" if pct is None or abs(pct) < ESTAVEL_PCT else ("melhorou" if vd > va else "piorou")
                linhas.append(_linha(f"Pico {lado} · {_medida_nome(m)}", va, vd, m.get("unidade") or "", direcao, "Força"))

    plat_antes = {_chave(p.get("teste", "") + " " + p.get("metrica", "")): p for p in antes.get("plataforma") or []}
    for p in depois.get("plataforma") or []:
        chave = _chave(p.get("teste", "") + " " + p.get("metrica", ""))
        a = plat_antes.get(chave)
        nome = " · ".join(x for x in (p.get("teste"), p.get("metrica")) if x) or "Plataforma"
        if a is None or a.get("assimetria_pct") is None or p.get("assimetria_pct") is None:
            continue
        ia, idp = a["assimetria_pct"], p["assimetria_pct"]
        direcao = "estavel" if abs(idp - ia) < 2 else ("melhorou" if idp < ia else "piorou")
        linhas.append(_linha(f"Assimetria {nome}", ia, idp, "%", direcao, "Plataforma de força"))

    for chave, a in anteriores.items():
        if chave not in vistos:
            linhas.append(_linha(f"LSI {_medida_nome(a)}", (a.get("pico") or {}).get("lsi_pct"), None, "%",
                                 "ausente", "Simetria"))
    return linhas


_SECOES_TEXTO = (
    ("Sinais de alerta", r"^##\s*0\."),
    ("Achados", r"^##\s*3\."),
    ("Impressão", r"^##\s*4\."),
    ("Síntese", r"^##\s*5\.\s*(S[IÍ]NTESE|(CLINICAL\s+)?(SYNTHESIS|SUMMARY))"),  # laudo em inglês: core/idioma.py
)


def secao(texto: str, padrao: str) -> str:
    linhas = (texto or "").replace("\r\n", "\n").split("\n")
    saida, dentro = [], False
    for linha in linhas:
        if re.match(r"^#{2}\s", linha.strip()):
            if dentro:
                break
            dentro = bool(re.match(padrao, linha.strip(), re.I))
            continue
        if dentro:
            saida.append(linha)
    return "\n".join(saida).strip()


def comparar_texto(texto_antes: str, texto_depois: str) -> list[dict]:
    blocos = []
    for nome, padrao in _SECOES_TEXTO:
        a, d = secao(texto_antes, padrao), secao(texto_depois, padrao)
        if a or d:
            blocos.append({"secao": nome, "antes": a, "depois": d, "igual": a == d})
    return blocos


def resumo(linhas: list[dict]) -> dict:
    contagem = {k: 0 for k in ("melhorou", "piorou", "estavel", "neutra", "nova", "ausente", "unidade_diferente")}
    for linha in linhas:
        contagem[linha["direcao"]] = contagem.get(linha["direcao"], 0) + 1
    return contagem


def destaques(linhas: list[dict], limite: int = 6) -> list[dict]:
    """O que o profissional deve ver primeiro: pioras, depois melhoras, pela variação."""
    peso = {"piorou": 0, "melhorou": 1}
    relevantes = [linha for linha in linhas if linha["direcao"] in peso]
    return sorted(relevantes, key=lambda x: (peso[x["direcao"]], -abs(x.get("delta_pct") or 0)))[:limite]


def comparar(tipo: str, antes: dict, depois: dict) -> dict:
    """antes/depois: {'id', 'created_at', 'dados' (estruturados ou None), 'texto'}."""
    if tipo == "laboratorial" and antes.get("dados") and depois.get("dados"):
        linhas = comparar_laboratorial(antes["dados"], depois["dados"])
    elif tipo == "dinamometria" and antes.get("dados") and depois.get("dados"):
        linhas = comparar_dinamometria(antes["dados"], depois["dados"])
    else:
        linhas = []
    return {
        "tipo": tipo,
        "linhas": linhas,
        "resumo": resumo(linhas),
        "destaques": destaques(linhas),
        "texto": comparar_texto(antes.get("texto") or "", depois.get("texto") or "") if not linhas else [],
    }


def pares_disponiveis(analises: list[dict]) -> dict[str, list[dict]]:
    """Tipo de exame → laudos do paciente (mais recente primeiro), só tipos com 2 ou mais."""
    por_tipo: dict[str, list[dict]] = {}
    for a in analises:
        tipo = a.get("tipo_exame") or "geral"
        if tipo == "correlacao":
            continue
        por_tipo.setdefault(tipo, []).append(a)
    return {
        tipo: sorted(itens, key=lambda a: a.get("created_at") or "", reverse=True)
        for tipo, itens in por_tipo.items() if len(itens) >= 2
    }
