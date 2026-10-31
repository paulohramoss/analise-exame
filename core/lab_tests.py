"""
Exames laboratoriais (sangue e bioquímica) com leitura voltada ao sistema musculoesquelético.

Como na dinamometria, a IA só LÊ o laudo do laboratório (extração em JSON). Quem
decide se um valor está fora da faixa é o código, comparando com a faixa de
referência impressa no PRÓPRIO laudo (cada laboratório e método tem a sua): nada
de faixa "de memória". Sem faixa no laudo, o resultado fica como "sem referência".

O laudo final junta:
    1. identificação          (montada aqui)
    2. resultados e situação  (tabela montada aqui: abaixo / dentro / acima da faixa)
    3–6. achados, impressão, impacto e conduta (texto da IA, com foco em
         inflamação, lesão muscular, metabolismo ósseo, ferro, hormônios etc.)
"""

import json
import re

from core.dynamometry import _fmt, _number, _text  # mesmos leitores tolerantes de número/texto

SITUACOES = {"abaixo": "↓ Abaixo", "acima": "↑ Acima", "normal": "Dentro", "sem_referencia": "Sem referência"}

# Grupos para organizar a tabela (o que não casar vai para "Outros").
PAINEIS = [
    ("Hemograma", r"hem[aá]c|eritr|hemoglob(?!ina glicada)|hemat[oó]c|vcm|hcm|chcm|rdw|leuc|neutr|linf|mon[oó]c|eosin|bas[oó]f|plaquet|segment|bast"),
    ("Inflamação", r"\bpcr\b|prote[ií]na c|vhs|hemossedimen|fibrinog|interleuc|il-6"),
    ("Músculo e fígado", r"\bck\b|\bcpk\b|creatin(o)?quinase|creatina quinase|\bldh\b|desidrogenase l[aá]tica|aldolase|mioglobina|\btgo\b|\bast\b|\btgp\b|\balt\b"),
    ("Metabolismo ósseo", r"vitamina d|25.?oh|c[aá]lcio|f[oó]sforo|pth|paratorm|fosfatase alcalina|magn[eé]sio"),
    ("Ferro", r"\bferro\b|ferritina|transferrina|satura[cç][aã]o|tibc"),
    ("Rim e eletrólitos", r"creatinina|ur[eé]ia|\bsódio\b|\bsodio\b|pot[aá]ssio|cloro|taxa de filtra|tfg"),
    ("Metabolismo", r"glicose|glicemia|hemoglobina glicada|hba1c|insulina|colesterol|\bhdl\b|\bldl\b|triglic|[aá]cido [uú]rico"),
    ("Hormônios", r"\btsh\b|\bt4\b|\bt3\b|testosterona|estradiol|cortisol|prolactina|\bigf\b|\bshbg\b"),
    ("Vitaminas", r"vitamina b12|cobalamina|[aá]cido f[oó]lico|folato"),
]


def build_extraction_prompt() -> str:
    return """Você recebeu o laudo de EXAMES LABORATORIAIS (sangue, bioquímica, hormônios).

TAREFA: transcreva cada resultado em JSON, exatamente como no laudo. Não interprete.
Use ponto como separador decimal. Campo ausente = null.

Formato (responda APENAS com o JSON):
{
  "laboratorio": "nome do laboratório, se aparecer",
  "data_coleta": "AAAA-MM-DD ou null",
  "resultados": [
    {
      "exame": "nome do analito, ex.: Creatinoquinase (CK), Proteína C reativa, Vitamina D (25-OH)",
      "valor": número ou null,
      "valor_texto": "o resultado como escrito, se não for número (ex.: 'Não reagente')",
      "unidade": "ex.: U/L, mg/dL, ng/mL",
      "referencia_min": número ou null,
      "referencia_max": número ou null,
      "referencia_texto": "a faixa de referência como escrita no laudo"
    }
  ]
}

Regras:
- Use a faixa de referência do PRÓPRIO laudo. Se a faixa for "até 5,0", referencia_min = null e
  referencia_max = 5.0; se for "maior que 30", referencia_min = 30.
- Se houver faixas por sexo ou idade, use a que o laudo aplica ao paciente; se não der para saber,
  deixe min/max null e copie o texto em referencia_texto.
- Ignore nome, documento, médico solicitante e qualquer dado que identifique o paciente."""


def parse_extraction(text: str) -> dict:
    """JSON da IA → resultados limpos. Levanta ValueError se não há resultado algum."""
    from core.annotations import _extract_json

    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("Resposta da extração não é um objeto JSON.")
    resultados = []
    for item in data.get("resultados") or []:
        if not isinstance(item, dict):
            continue
        exame = _text(item.get("exame"), 90)
        valor = _number(item.get("valor"))
        valor_texto = _text(item.get("valor_texto"), 60)
        if not exame or (valor is None and not valor_texto):
            continue
        resultados.append({
            "exame": exame,
            "valor": valor,
            "valor_texto": valor_texto,
            "unidade": _text(item.get("unidade"), 20),
            "referencia_min": _number(item.get("referencia_min")),
            "referencia_max": _number(item.get("referencia_max")),
            "referencia_texto": _text(item.get("referencia_texto"), 120),
        })
    if not resultados:
        raise ValueError("Nenhum resultado laboratorial encontrado no laudo.")
    return {
        "laboratorio": _text(data.get("laboratorio"), 80),
        "data_coleta": _text(data.get("data_coleta"), 10) or None,
        "resultados": resultados[:120],
    }


def situacao(resultado: dict) -> str:
    """abaixo / acima / normal / sem_referencia, pela faixa impressa no laudo."""
    valor, minimo, maximo = resultado["valor"], resultado["referencia_min"], resultado["referencia_max"]
    if valor is None or (minimo is None and maximo is None):
        return "sem_referencia"
    if minimo is not None and valor < minimo:
        return "abaixo"
    if maximo is not None and valor > maximo:
        return "acima"
    return "normal"


def painel(exame: str) -> str:
    return next((nome for nome, pattern in PAINEIS if re.search(pattern, exame, re.I)), "Outros")


# ── Índices calculados e padrões entre exames ─────────────────────────────────
#
# Regras fixas e auditáveis, sem IA: o profissional vê de onde veio cada número e
# cada padrão. Os padrões são "compatíveis com", nunca diagnóstico.

def _find(resultados: list[dict], pattern: str, exclude: str | None = None) -> dict | None:
    for r in resultados:
        if r.get("valor") is None or not re.search(pattern, r["exame"], re.I):
            continue
        if exclude and re.search(exclude, r["exame"], re.I):
            continue
        return r
    return None


def _unit(r: dict | None) -> str:
    return (r or {}).get("unidade", "").lower().replace("µ", "u").replace(" ", "")


def _is(r: dict | None, situacao_: str) -> bool:
    return bool(r) and r.get("situacao") == situacao_


def _indice(nome, valor, unidade, referencia, alterado, formula, exames):
    return {"nome": nome, "valor": round(valor, 2), "unidade": unidade, "referencia": referencia,
            "alterado": alterado, "formula": formula, "exames": exames}


def derive_indices(resultados: list[dict]) -> list[dict]:
    indices = []
    glicose = _find(resultados, r"glicose|glicemia", r"glicada|hba1c|urina")
    insulina = _find(resultados, r"insulina")
    # µUI/mL e mUI/L são numericamente iguais; outra unidade de insulina não entra na fórmula.
    if glicose and insulina and ("/ml" in _unit(insulina) or "/l" in _unit(insulina)):
        g = glicose["valor"] * (18.0 if "mmol" in _unit(glicose) else 1.0)  # mmol/L → mg/dL
        homa = g * insulina["valor"] / 405
        indices.append(_indice("HOMA-IR (resistência à insulina)", homa, "", "< 2,71", homa >= 2.71,
                               "glicose (mg/dL) × insulina (µUI/mL) ÷ 405", [glicose["exame"], insulina["exame"]]))
    tg = _find(resultados, r"triglic")
    hdl = _find(resultados, r"\bhdl\b")
    ct = _find(resultados, r"colesterol total|^colesterol$")
    if tg and hdl and "mg" in _unit(tg) and "mg" in _unit(hdl) and hdl["valor"] > 0:
        razao = tg["valor"] / hdl["valor"]
        indices.append(_indice("Triglicerídeos / HDL", razao, "", "< 3,5", razao >= 3.5,
                               "triglicerídeos ÷ HDL (mg/dL)", [tg["exame"], hdl["exame"]]))
    if ct and hdl and "mg" in _unit(ct) and "mg" in _unit(hdl):
        nao_hdl = ct["valor"] - hdl["valor"]
        indices.append(_indice("Colesterol não-HDL", nao_hdl, "mg/dL", "meta conforme o risco cardiovascular (em geral < 130)",
                               nao_hdl >= 160, "colesterol total − HDL", [ct["exame"], hdl["exame"]]))
    ferro = _find(resultados, r"\bferro\b", r"capacidade|satura")
    transferrina = _find(resultados, r"transferrina", r"satura")
    ja_tem_saturacao = _find(resultados, r"satura[cç][aã]o")
    if ferro and transferrina and not ja_tem_saturacao and "mg/dl" in _unit(transferrina) and transferrina["valor"] > 0:
        sat = ferro["valor"] / (transferrina["valor"] * 1.25) * 100  # CTLF ≈ transferrina (mg/dL) × 1,25
        indices.append(_indice("Saturação de transferrina (estimada)", sat, "%", "20 a 45%", sat < 20 or sat > 45,
                               "ferro (µg/dL) ÷ (transferrina (mg/dL) × 1,25) × 100", [ferro["exame"], transferrina["exame"]]))
    return indices


def _padrao(titulo, nivel, exames, explicacao, sugestao):
    return {"titulo": titulo, "nivel": nivel, "exames": exames, "explicacao": explicacao, "sugestao": sugestao}


def derive_patterns(resultados: list[dict], indices: list[dict]) -> list[dict]:
    """Combinações de resultados com significado clínico conhecido (ordem: atenção primeiro)."""
    R = {
        "hb": _find(resultados, r"hemoglobina", r"glicada|hba1c|corpuscular"),
        "ferritina": _find(resultados, r"ferritina"),
        "pcr": _find(resultados, r"prote[ií]na c|\bpcr\b"),
        "ck": _find(resultados, r"creatin(o)?quinase|creatina quinase|\bck\b|\bcpk\b"),
        "tgo": _find(resultados, r"\btgo\b|\bast\b|aspartato"),
        "tgp": _find(resultados, r"\btgp\b|\balt\b|alanina"),
        "tsh": _find(resultados, r"\btsh\b"),
        "t4": _find(resultados, r"\bt4\b|tiroxina"),
        "glicose": _find(resultados, r"glicose|glicemia", r"glicada|urina"),
        "insulina": _find(resultados, r"insulina"),
        "vitd": _find(resultados, r"vitamina d|25.?oh"),
        "b12": _find(resultados, r"b12|cobalamina"),
        "testo": _find(resultados, r"testosterona", r"livre"),
        "cortisol": _find(resultados, r"cortisol"),
    }
    nomes = lambda *keys: [R[k]["exame"] for k in keys if R.get(k)]  # noqa: E731
    padroes = []

    if _is(R["ferritina"], "abaixo") and _is(R["hb"], "abaixo"):
        padroes.append(_padrao("Compatível com anemia por deficiência de ferro", "atencao", nomes("hb", "ferritina"),
                               "Hemoglobina e ferritina abaixo da faixa: o estoque de ferro baixo explica a anemia.",
                               "Investigar a causa (ingestão, perdas, absorção) com o médico."))
    elif _is(R["ferritina"], "abaixo"):
        padroes.append(_padrao("Estoque de ferro baixo, ainda sem anemia", "atencao", nomes("ferritina", "hb"),
                               "Ferritina abaixo da faixa com hemoglobina preservada: fase inicial da deficiência de ferro.",
                               "Pode causar fadiga e pior recuperação; vale corrigir antes de evoluir para anemia."))
    elif _is(R["hb"], "abaixo"):
        padroes.append(_padrao("Hemoglobina baixa sem depleção de ferro evidente", "atencao", nomes("hb", "ferritina"),
                               "Anemia com ferritina normal sugere outra causa (inflamação, B12/folato, perdas recentes).",
                               "Correlacionar com B12, folato, reticulócitos e contexto clínico."))
    if _is(R["ferritina"], "acima") and _is(R["pcr"], "acima"):
        padroes.append(_padrao("Ferritina elevada com inflamação", "info", nomes("ferritina", "pcr"),
                               "A ferritina sobe na inflamação (proteína de fase aguda); não indica excesso de ferro por si só.",
                               "Reavaliar após a resolução do quadro inflamatório."))

    if _is(R["ck"], "acima"):
        com_transaminases = _is(R["tgo"], "acima") or _is(R["tgp"], "acima")
        padroes.append(_padrao(
            "Compatível com lesão ou sobrecarga muscular" + (" (CK com transaminases)" if com_transaminases else ""),
            "atencao", nomes("ck", "tgo", "tgp", "pcr"),
            "CK acima da faixa" + (", acompanhada de TGO/TGP, que também saem do músculo" if com_transaminases else "")
            + (". PCR normal: sem inflamação sistêmica." if _is(R["pcr"], "normal") else "."),
            "Exercício intenso nas últimas 48–72 h eleva CK, TGO e LDH: repetir em repouso antes de concluir."))
    elif _is(R["tgp"], "acima") or _is(R["tgo"], "acima"):
        padroes.append(_padrao("Transaminases elevadas", "atencao", nomes("tgo", "tgp", "ck"),
                               "TGO/TGP acima da faixa com CK normal: a origem hepática é mais provável que a muscular.",
                               "Avaliar fígado (álcool, medicamentos, esteatose) com o médico."))

    if R["tsh"] and R["t4"]:
        if _is(R["tsh"], "acima"):
            padroes.append(_padrao("Compatível com hipotireoidismo" + (" subclínico" if _is(R["t4"], "normal") else ""),
                                   "atencao", nomes("tsh", "t4"),
                                   "TSH acima da faixa" + (" com T4 normal." if _is(R["t4"], "normal") else " com T4 baixo."),
                                   "Pode causar fadiga, ganho de peso e dor muscular; confirmar com nova dosagem."))
        elif _is(R["tsh"], "abaixo"):
            padroes.append(_padrao("Compatível com hipertireoidismo" + (" subclínico" if _is(R["t4"], "normal") else ""),
                                   "atencao", nomes("tsh", "t4"),
                                   "TSH abaixo da faixa" + (" com T4 normal." if _is(R["t4"], "normal") else " com T4 alto."),
                                   "Pode causar perda de massa muscular e palpitações; confirmar com nova dosagem."))

    homa = next((i for i in indices if i["nome"].startswith("HOMA")), None)
    if (homa and homa["alterado"]) or _is(R["insulina"], "acima"):
        padroes.append(_padrao("Sinais de resistência à insulina", "atencao", nomes("glicose", "insulina"),
                               f"HOMA-IR de {_num(homa['valor'])} (referência < 2,71)." if homa else "Insulina acima da faixa.",
                               "Correlacionar com circunferência abdominal, triglicerídeos e HDL; reforçar atividade física e dieta."))
    razao = next((i for i in indices if i["nome"].startswith("Triglicerídeos")), None)
    if razao and razao["alterado"]:
        padroes.append(_padrao("Perfil lipídico aterogênico", "atencao", razao["exames"],
                               f"Triglicerídeos/HDL de {_num(razao['valor'])} (alvo < 3,5).",
                               "Associado a resistência à insulina e risco cardiovascular."))

    if _is(R["vitd"], "abaixo"):
        grave = R["vitd"]["valor"] < 20
        padroes.append(_padrao("Vitamina D " + ("deficiente" if grave else "insuficiente"), "atencao", nomes("vitd"),
                               f"{_num(R['vitd']['valor'])} {R['vitd']['unidade']}"
                               + (" (abaixo de 20 ng/mL)." if grave else "."),
                               "Relevante para massa óssea, força muscular e consolidação de lesões."))
    if _is(R["b12"], "abaixo"):
        padroes.append(_padrao("Vitamina B12 baixa", "atencao", nomes("b12", "hb"),
                               "Pode causar fadiga, formigamento e anemia.",
                               "Investigar ingestão e absorção (ex.: uso de metformina ou omeprazol)."))
    if _is(R["testo"], "abaixo"):
        com_cortisol = _is(R["cortisol"], "acima")
        padroes.append(_padrao("Testosterona baixa" + (" com cortisol alto" if com_cortisol else ""), "atencao",
                               nomes("testo", "cortisol"),
                               "Relação anabólica/catabólica desfavorável: pode refletir sobrecarga de treino, sono ruim ou estresse."
                               if com_cortisol else "Pode reduzir recuperação, massa muscular e disposição.",
                               "Repetir pela manhã (até as 9h); avaliar sono, carga de treino e déficit calórico."))
    if _is(R["pcr"], "acima") and not any("inflama" in p["titulo"].lower() for p in padroes):
        padroes.append(_padrao("Inflamação ativa", "info", nomes("pcr"),
                               "PCR acima da faixa: processo inflamatório ou infeccioso em curso.",
                               "Correlacionar com sintomas; lesões agudas e infecções elevam a PCR."))
    return sorted(padroes, key=lambda p: p["nivel"] != "atencao")


def compute(dados: dict) -> dict:
    resultados = []
    for r in dados["resultados"]:
        calc = dict(r, situacao=situacao(r), painel=painel(r["exame"]))
        # Quanto o valor se afasta do limite violado (ex.: CK 3× o limite superior).
        if calc["situacao"] == "acima" and r["referencia_max"]:
            calc["vezes_o_limite"] = round(r["valor"] / r["referencia_max"], 1)
        resultados.append(calc)
    ordem = [nome for nome, _ in PAINEIS] + ["Outros"]
    resultados.sort(key=lambda r: ordem.index(r["painel"]))
    alterados = [r for r in resultados if r["situacao"] in {"abaixo", "acima"}]
    indices = derive_indices(resultados)
    return {**dados, "resultados": resultados, "total": len(resultados), "alterados": len(alterados),
            "indices": indices, "padroes": derive_patterns(resultados, indices)}


def _cell(text) -> str:
    return str(text).replace("|", "/")


def format_identification(calc: dict) -> str:
    linhas = ["## 1. IDENTIFICAÇÃO DO EXAME", "- Tipo: Exames laboratoriais (sangue e bioquímica)"]
    if calc.get("laboratorio"):
        linhas.append(f"- Laboratório: {_cell(calc['laboratorio'])}")
    if calc.get("data_coleta"):
        linhas.append(f"- Data da coleta: {calc['data_coleta']}")
    linhas.append(f"- Resultados lidos: {calc['total']} ({calc['alterados']} fora da faixa de referência do laudo)")
    return "\n".join(linhas)


def _num(value) -> str:
    """Número como no laudo: até 2 casas, sem zeros à direita, vírgula decimal."""
    if value is None:
        return "—"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")  # 245000 → 245,000.00 → 245,000
    return text.replace(",", "_").replace(".", ",").replace("_", ".")  # → 245.000


def _reference(r: dict) -> str:
    if r.get("referencia_texto"):
        return r["referencia_texto"]
    minimo, maximo = r.get("referencia_min"), r.get("referencia_max")
    if minimo is not None and maximo is not None:
        return f"{_num(minimo)} a {_num(maximo)}"
    if maximo is not None:
        return f"até {_num(maximo)}"
    if minimo is not None:
        return f"acima de {_num(minimo)}"
    return "—"


def format_results(calc: dict) -> str:
    partes = ["## 2. RESULTADOS E SITUAÇÃO",
              "> A situação compara cada valor com a faixa de referência impressa no próprio laudo do laboratório.",
              "", "| Painel | Exame | Resultado | Referência do laudo | Situação |", "|---|---|---|---|---|"]
    for r in calc["resultados"]:
        un = f" {r['unidade']}" if r.get("unidade") else ""
        valor = f"{_num(r['valor'])}{un}" if r["valor"] is not None else r["valor_texto"]
        ref = _reference(r)
        sit = SITUACOES[r["situacao"]]
        if r.get("vezes_o_limite") and r["vezes_o_limite"] >= 1.5:
            sit += f" ({_fmt(r['vezes_o_limite'], 1)}× o limite)"
        if r["situacao"] in {"abaixo", "acima"}:
            sit = f"**{sit}**"
        partes.append(f"| {r['painel']} | {_cell(r['exame'])} | {_cell(valor)} | {_cell(ref)} | {sit} |")
    if calc.get("indices"):
        partes += ["", "### Índices calculados pelo sistema", "",
                   "| Índice | Valor | Referência | Cálculo |", "|---|---|---|---|"]
        for i in calc["indices"]:
            valor = f"{_num(i['valor'])}{' ' + i['unidade'] if i['unidade'] else ''}"
            partes.append(f"| {i['nome']} | {'**' + valor + '**' if i['alterado'] else valor} | {i['referencia']} | {i['formula']} |")
    if calc.get("padroes"):
        partes += ["", "### Padrões identificados entre os exames", ""]
        for p in calc["padroes"]:
            partes.append(f"- **{p['titulo']}** ({', '.join(p['exames'])}): {p['explicacao']} {p['sugestao']}")
        partes.append("> Padrões por regras fixas sobre os valores do laudo: indicam o que investigar, não são diagnóstico.")
    return "\n".join(partes)


def build_interpretation_prompt(calc: dict, description: str) -> str:
    resumo = json.dumps(calc, ensure_ascii=False, default=str)[:12000]
    return f"""Você é médico com experiência em medicina esportiva e do aparelho locomotor, da plataforma Three Health.
Abaixo estão resultados laboratoriais. A coluna "situacao" (abaixo/acima/normal/sem_referencia) JÁ FOI
definida pelo sistema com a faixa de referência do próprio laudo: não a altere nem invente faixas.

HISTÓRICO CLÍNICO (pode estar vazio): {description or "Não informado"}

RESULTADOS (JSON): {resumo}

Escreva SOMENTE as seções abaixo, com estes títulos exatos, em português, sem introdução:

## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
Liste os resultados alterados e as combinações relevantes. **[FORTE]** para alterações claras e
consistentes entre si (ex.: CK muito acima do limite com TGO/LDH elevados); **[MODERADO]** para alterações
leves, isoladas ou que dependem de contexto (ex.: hidratação, exercício recente, jejum).
Termine com "> **Achados descartados por evidência insuficiente:** ..." (ou "Nenhum achado descartado.").

## 4. IMPRESSÃO LABORATORIAL
Síntese por eixo, com foco musculoesquelético e esportivo: lesão/sobrecarga muscular, inflamação,
metabolismo ósseo (vitamina D, cálcio, PTH), ferro e anemia, função renal, metabolismo e hormônios.

## 5. IMPACTO NA RECUPERAÇÃO E NO DESEMPENHO
Como os resultados podem influenciar dor, recuperação de lesões, consolidação óssea, fadiga e
treinamento, correlacionando com o histórico clínico quando houver.

## 6. CONDUTA E PRÓXIMOS PASSOS
Exames a repetir ou complementar (e quando), fatores a controlar na próxima coleta (ex.: 48–72 h sem
treino intenso antes de dosar CK) e quando encaminhar a outro especialista. Sem prescrever medicamentos
ou doses.

Seja objetivo (até 700 palavras). Lembre que exercício intenso recente eleva CK, TGO, LDH e às vezes
PCR: diga isso quando for pertinente."""


def compose_report(calc: dict, interpretation: str | None) -> str:
    partes = [format_identification(calc), "", format_results(calc), ""]
    texto = (interpretation or "").strip()
    if texto:
        inicio = texto.find("## 3.")
        partes.append(texto[inicio:] if inicio >= 0 else texto)
    else:
        partes.append("## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA\n"
                      "> Interpretação automática indisponível no momento. A tabela acima mostra cada "
                      "resultado comparado à faixa do laudo; revise-a diretamente.")
    return "\n".join(partes).strip()


# ── Painel visual (dados para o template _painel_laboratorial.html) ───────────

def _bar(r: dict) -> dict | None:
    """Posições (0–100%) da faixa de referência e do valor numa régua horizontal."""
    valor, minimo, maximo = r.get("valor"), r.get("referencia_min"), r.get("referencia_max")
    if valor is None or (minimo is None and maximo is None):
        return None
    if minimo is not None and maximo is not None:
        folga = (maximo - minimo) * 0.6 or abs(maximo) * 0.3 or 1
        inicio, fim = max(0.0, minimo - folga) if minimo >= 0 else minimo - folga, maximo + folga
    elif maximo is not None:
        inicio, fim = 0.0, maximo * 1.8 or 1
    else:
        inicio, fim = 0.0, minimo * 2.2 or 1
    if valor > fim:
        fim = valor * 1.08
    if valor < inicio:
        inicio = valor * 0.92 if valor > 0 else valor - abs(fim - valor) * 0.08
    largura = (fim - inicio) or 1

    def pos(x):
        return round(min(100.0, max(0.0, (x - inicio) / largura * 100)), 1)

    return {
        "faixa_inicio": pos(minimo) if minimo is not None else 0.0,
        "faixa_fim": pos(maximo) if maximo is not None else 100.0,
        "valor": pos(valor),
    }


def dashboard(calc: dict | None) -> dict | None:
    """Resumo visual dos exames laboratoriais: painéis, réguas, índices e padrões."""
    if not calc or not calc.get("resultados"):
        return None
    paineis: dict[str, dict] = {}
    for r in calc["resultados"]:
        painel_ = paineis.setdefault(r["painel"], {"nome": r["painel"], "itens": [], "alterados": 0})
        un = f" {r['unidade']}" if r.get("unidade") else ""
        painel_["itens"].append({
            "exame": r["exame"],
            "valor": f"{_num(r['valor'])}{un}" if r.get("valor") is not None else (r.get("valor_texto") or "—"),
            "referencia": _reference(r),
            "situacao": r["situacao"],
            "rotulo": SITUACOES[r["situacao"]],
            "vezes_o_limite": r.get("vezes_o_limite"),
            "barra": _bar(r),
        })
        if r["situacao"] in {"abaixo", "acima"}:
            painel_["alterados"] += 1
    return {
        "total": calc.get("total", 0),
        "alterados": calc.get("alterados", 0),
        "paineis": list(paineis.values()),
        "indices": [{**i, "valor_fmt": f"{_num(i['valor'])}{' ' + i['unidade'] if i['unidade'] else ''}"}
                    for i in calc.get("indices") or []],
        "padroes": calc.get("padroes") or [],
        "data_coleta": calc.get("data_coleta"),
        "laboratorio": calc.get("laboratorio"),
    }
