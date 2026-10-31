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
    ("Músculo", r"\bck\b|\bcpk\b|creatin(o)?quinase|creatina quinase|\bldh\b|desidrogenase l[aá]tica|aldolase|mioglobina|\btgo\b|\bast\b|\btgp\b|\balt\b"),
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
    return {**dados, "resultados": resultados, "total": len(resultados), "alterados": len(alterados)}


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
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


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
