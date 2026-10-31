"""
Dinamometria: isocinética, isométrica (portátil/célula de carga) e plataforma de força.

A IA só LÊ o relatório do aparelho (extração em JSON). Toda conta — déficit
entre lados, índice de simetria, torque por peso, relações agonista/antagonista,
assimetria de salto — é feita aqui, em código: modelo de linguagem erra
aritmética, e um déficit errado muda a conduta (ex.: liberar ou não o retorno
ao esporte). A interpretação clínica recebe os valores já calculados e é
instruída a não recalculá-los.

O laudo final junta:
    1. identificação do exame         (montada aqui)
    2. resultados medidos e calculados (tabelas montadas aqui)
    3–6. achados, impressão, impacto e conduta (texto da IA)

Referências usadas nas classificações (valores de uso corrente na literatura
de reabilitação; o laudo deixa claro que dependem do protocolo):
- déficit bilateral: < 10% simétrico; 10–15% leve; 15–20% moderado; ≥ 20% importante;
  índice de simetria (LSI) ≥ 90% é o critério mais usado para retorno ao esporte.
- isquiotibiais:quadríceps convencional (concêntrico/concêntrico) < 0,60 e
  funcional (excêntrico isquio / concêntrico quadríceps) < 1,0 sinalizam desequilíbrio.
- rotadores externos:internos do ombro < 0,66.
- assimetria em plataforma de força ≥ 10%.
"""

import json
import re

TIPOS = {
    "isocinetico": "Dinamometria isocinética",
    "isometrico": "Dinamometria isométrica",
    "plataforma": "Plataforma de força",
}

# (nome da relação, padrão do grupo do numerador, padrão do denominador, ação do numerador,
#  ação do denominador, limite inferior de referência ou None)
RATIOS = [
    ("Isquiotibiais:Quadríceps convencional", r"isquio|flex(ores|ão)\s+(do\s+)?joelho|hamstring",
     r"quadr[ií]ceps|extens(ores|ão)\s+(do\s+)?joelho", "concentrica", "concentrica", 0.60),
    ("Isquiotibiais:Quadríceps funcional", r"isquio|flex(ores|ão)\s+(do\s+)?joelho|hamstring",
     r"quadr[ií]ceps|extens(ores|ão)\s+(do\s+)?joelho", "excentrica", "concentrica", 1.0),
    ("Isquiotibiais:Quadríceps isométrico", r"isquio|flex(ores|ão)\s+(do\s+)?joelho|hamstring",
     r"quadr[ií]ceps|extens(ores|ão)\s+(do\s+)?joelho", "isometrica", "isometrica", None),
    ("Rotadores externos:internos", r"rota(dores|ção)\s+extern", r"rota(dores|ção)\s+intern", None, None, 0.66),
    ("Abdutores:Adutores do quadril", r"abdu(tores|ção)", r"adu(tores|ção)", None, None, None),
    ("Eversores:Inversores do tornozelo", r"evers", r"invers", None, None, None),
]


# ── Extração ──────────────────────────────────────────────────────────────────

def build_extraction_prompt() -> str:
    return """Você recebeu o relatório de um exame de DINAMOMETRIA (isocinético como Biodex/Cybex/Humac,
isométrico portátil como MicroFET/Kinvent/ForceFrame, ou plataforma de força como ForceDecks/Hawkin).

TAREFA: transcreva os valores do relatório em JSON. Não interprete e não calcule nada:
copie os números como estão (use ponto como separador decimal). Campo ausente = null.

Formato (responda APENAS com o JSON):
{
  "tipo_teste": "isocinetico" | "isometrico" | "plataforma",
  "aparelho": "nome do equipamento, se aparecer",
  "protocolo": "resumo do protocolo: velocidades, ângulo, posição, repetições",
  "data_exame": "AAAA-MM-DD ou null",
  "massa_corporal_kg": número ou null,
  "lado_acometido": "direito" | "esquerdo" | null,
  "medidas": [
    {
      "grupo_muscular": "ex.: Quadríceps, Isquiotibiais, Rotadores externos do ombro",
      "articulacao": "ex.: Joelho",
      "acao": "concentrica" | "excentrica" | "isometrica",
      "velocidade_graus_s": número ou null,
      "angulo_graus": número ou null,
      "unidade": "Nm" | "N" | "kgf",
      "pico_direito": número ou null,
      "pico_esquerdo": número ou null,
      "trabalho_direito": número ou null,
      "trabalho_esquerdo": número ou null,
      "potencia_direito": número ou null,
      "potencia_esquerdo": número ou null,
      "fadiga_direito_pct": número ou null,
      "fadiga_esquerdo_pct": número ou null
    }
  ],
  "plataforma": [
    {"teste": "ex.: CMJ, salto unipodal, IMTP", "metrica": "ex.: altura do salto, pico de força, RFD",
     "unidade": "ex.: cm, N, N/s", "valor": número ou null, "esquerdo": número ou null, "direito": número ou null}
  ],
  "observacoes": "qualquer informação relevante do relatório (dor, compensação, testes interrompidos)"
}

Regras:
- Uma entrada em "medidas" por grupo muscular + ação + velocidade (ou ângulo).
- Se o relatório trouxer só déficit/percentual sem os valores de cada lado, deixe os picos null
  e registre o percentual em "observacoes".
- Ignore nome, documento e qualquer dado que identifique o paciente."""


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("−", "-")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", text):  # 1.234,5
        text = text.replace(".", "").replace(",", ".")
    text = text.replace(",", ".")
    match = re.search(r"-?\d+(\.\d+)?", text)
    return float(match.group(0)) if match else None


def _text(value, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _action(value) -> str | None:
    text = _text(value).lower()
    if text.startswith(("conc", "con")):
        return "concentrica"
    if text.startswith(("exc", "ecc")):
        return "excentrica"
    if text.startswith(("isom", "isometr")):
        return "isometrica"
    return None


def _side(value) -> str | None:
    text = _text(value).lower()
    if text in {"d", "r"} or text.startswith(("dir", "right")):
        return "direito"
    if text in {"e", "l"} or text.startswith(("esq", "left")):
        return "esquerdo"
    return None


def parse_extraction(text: str) -> dict:
    """JSON da IA → dados limpos. Levanta ValueError se não há nenhuma medida utilizável."""
    from core.annotations import _extract_json  # mesmo leitor tolerante (bloco ```json, texto ao redor)

    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("Resposta da extração não é um objeto JSON.")
    tipo = _text(data.get("tipo_teste")).lower()
    tipo = next((key for key in TIPOS if tipo.startswith(key[:5])), "isocinetico")

    medidas = []
    for item in data.get("medidas") or []:
        if not isinstance(item, dict):
            continue
        medida = {
            "grupo_muscular": _text(item.get("grupo_muscular"), 80),
            "articulacao": _text(item.get("articulacao"), 60),
            "acao": _action(item.get("acao")) or ("isometrica" if tipo == "isometrico" else None),
            "velocidade_graus_s": _number(item.get("velocidade_graus_s")),
            "angulo_graus": _number(item.get("angulo_graus")),
            "unidade": _text(item.get("unidade"), 10) or ("Nm" if tipo == "isocinetico" else "N"),
        }
        for campo in ("pico", "trabalho", "potencia"):
            for lado in ("direito", "esquerdo"):
                medida[f"{campo}_{lado}"] = _number(item.get(f"{campo}_{lado}"))
        for lado in ("direito", "esquerdo"):
            medida[f"fadiga_{lado}_pct"] = _number(item.get(f"fadiga_{lado}_pct"))
        if medida["grupo_muscular"] and (medida["pico_direito"] is not None or medida["pico_esquerdo"] is not None):
            medidas.append(medida)

    plataforma = []
    for item in data.get("plataforma") or []:
        if not isinstance(item, dict):
            continue
        linha = {
            "teste": _text(item.get("teste"), 60),
            "metrica": _text(item.get("metrica"), 80),
            "unidade": _text(item.get("unidade"), 12),
            "valor": _number(item.get("valor")),
            "esquerdo": _number(item.get("esquerdo")),
            "direito": _number(item.get("direito")),
        }
        if linha["metrica"] and any(linha[k] is not None for k in ("valor", "esquerdo", "direito")):
            plataforma.append(linha)

    if not medidas and not plataforma:
        raise ValueError("Nenhum valor de força encontrado no relatório.")
    massa = _number(data.get("massa_corporal_kg"))
    return {
        "tipo_teste": "plataforma" if plataforma and not medidas else tipo,
        "aparelho": _text(data.get("aparelho"), 80),
        "protocolo": _text(data.get("protocolo"), 400),
        "data_exame": _text(data.get("data_exame"), 10) or None,
        "massa_corporal_kg": massa if massa and 20 <= massa <= 250 else None,
        "lado_acometido": _side(data.get("lado_acometido")),
        "medidas": medidas,
        "plataforma": plataforma,
        "observacoes": _text(data.get("observacoes"), 800),
    }


# ── Cálculos ──────────────────────────────────────────────────────────────────

def classify_deficit(deficit: float | None) -> str:
    if deficit is None:
        return "—"
    if deficit < 10:
        return "Simétrico"
    if deficit < 15:
        return "Assimetria leve"
    if deficit < 20:
        return "Assimetria moderada"
    return "Assimetria importante"


def bilateral(direito: float | None, esquerdo: float | None, lado_acometido: str | None) -> dict | None:
    """Índice de simetria (LSI) e déficit.

    Com lado acometido informado: LSI = acometido / não acometido × 100 (pode
    passar de 100%). Sem ele: lado mais fraco / mais forte × 100.
    """
    if not direito or not esquerdo or direito <= 0 or esquerdo <= 0:
        return None
    if lado_acometido in {"direito", "esquerdo"}:
        acometido, outro = (direito, esquerdo) if lado_acometido == "direito" else (esquerdo, direito)
        lsi = acometido / outro * 100
        referencia = lado_acometido
    else:
        lsi = min(direito, esquerdo) / max(direito, esquerdo) * 100
        referencia = "direito" if direito < esquerdo else "esquerdo" if esquerdo < direito else None
    deficit = max(0.0, 100 - lsi)
    return {
        "lsi_pct": round(lsi, 1),
        "deficit_pct": round(deficit, 1),
        "lado_mais_fraco": referencia if deficit > 0 else None,
        "classificacao": classify_deficit(deficit),
        "criterio_retorno": lsi >= 90,
    }


def _speed_key(medida: dict) -> tuple:
    return (medida.get("velocidade_graus_s"), medida.get("angulo_graus"))


def compute(dados: dict) -> dict:
    """Acrescenta aos dados extraídos todos os valores derivados."""
    massa = dados.get("massa_corporal_kg")
    lado = dados.get("lado_acometido")
    medidas = []
    for medida in dados["medidas"]:
        calc = dict(medida)
        calc["pico"] = bilateral(medida["pico_direito"], medida["pico_esquerdo"], lado)
        calc["trabalho"] = bilateral(medida.get("trabalho_direito"), medida.get("trabalho_esquerdo"), lado)
        calc["potencia"] = bilateral(medida.get("potencia_direito"), medida.get("potencia_esquerdo"), lado)
        if massa and medida["unidade"].lower() in {"nm", "n.m", "n·m", "n"}:
            unidade = "Nm/kg" if medida["unidade"].lower() != "n" else "N/kg"
            calc["normalizado"] = {
                "unidade": unidade,
                "direito": round(medida["pico_direito"] / massa, 2) if medida["pico_direito"] else None,
                "esquerdo": round(medida["pico_esquerdo"] / massa, 2) if medida["pico_esquerdo"] else None,
            }
        medidas.append(calc)

    relacoes = []
    for nome, num_pat, den_pat, num_acao, den_acao, limite in RATIOS:
        nums = [m for m in dados["medidas"] if re.search(num_pat, m["grupo_muscular"], re.I)
                and (num_acao is None or m["acao"] == num_acao)]
        dens = [m for m in dados["medidas"] if re.search(den_pat, m["grupo_muscular"], re.I)
                and (den_acao is None or m["acao"] == den_acao)]
        for num in nums:
            den = next((d for d in dens if _speed_key(d) == _speed_key(num) and d is not num), None)
            if den is None:
                continue
            valores = {}
            for lado_ in ("direito", "esquerdo"):
                a, b = num[f"pico_{lado_}"], den[f"pico_{lado_}"]
                valores[lado_] = round(a / b, 2) if a and b else None
            if not any(valores.values()):
                continue
            abaixo = [lado_ for lado_, v in valores.items() if limite is not None and v is not None and v < limite]
            relacoes.append({
                "relacao": nome,
                "velocidade_graus_s": num["velocidade_graus_s"],
                "angulo_graus": num["angulo_graus"],
                **valores,
                "referencia_minima": limite,
                "abaixo_da_referencia": abaixo,
            })

    plataforma = []
    for linha in dados["plataforma"]:
        calc = dict(linha)
        e, d = linha["esquerdo"], linha["direito"]
        if e and d and max(e, d) > 0:
            assim = abs(d - e) / max(e, d) * 100
            calc["assimetria_pct"] = round(assim, 1)
            calc["lado_menor"] = "esquerdo" if e < d else "direito" if d < e else None
            calc["assimetria_relevante"] = assim >= 10
        plataforma.append(calc)

    return {**dados, "medidas": medidas, "relacoes": relacoes, "plataforma": plataforma}


# ── Laudo ─────────────────────────────────────────────────────────────────────

def _fmt(value, decimals: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    text = f"{value:.{decimals}f}".replace(".", ",")
    if decimals and text.endswith("," + "0" * decimals):
        text = text.split(",")[0]
    return f"{text}{suffix}"


def _condition(medida: dict) -> str:
    acao = {"concentrica": "concêntrica", "excentrica": "excêntrica", "isometrica": "isométrica"}.get(medida.get("acao"), "")
    if medida.get("velocidade_graus_s"):
        return f"{acao} {_fmt(medida['velocidade_graus_s'], 0)}°/s".strip()
    if medida.get("angulo_graus") is not None:
        return f"{acao} a {_fmt(medida['angulo_graus'], 0)}°".strip()
    return acao or "—"


def _cell(text: str) -> str:
    return str(text).replace("|", "/")


def format_identification(dados: dict) -> str:
    linhas = ["## 1. IDENTIFICAÇÃO DO EXAME", f"- Tipo: {TIPOS[dados['tipo_teste']]}"]
    if dados.get("aparelho"):
        linhas.append(f"- Equipamento: {_cell(dados['aparelho'])}")
    if dados.get("protocolo"):
        linhas.append(f"- Protocolo: {_cell(dados['protocolo'])}")
    if dados.get("data_exame"):
        linhas.append(f"- Data do exame: {dados['data_exame']}")
    linhas.append(f"- Massa corporal: {_fmt(dados['massa_corporal_kg'], 1, ' kg') if dados.get('massa_corporal_kg') else 'não informada (sem normalização por peso)'}")
    lado = dados.get("lado_acometido")
    linhas.append(f"- Lado acometido: {lado if lado else 'não informado — simetria calculada como lado mais fraco / mais forte'}")
    return "\n".join(linhas)


def format_results(calc: dict) -> str:
    """Tabelas com os valores do relatório e os calculados pelo sistema."""
    partes = ["## 2. RESULTADOS — VALORES MEDIDOS E CALCULADOS",
              "> Déficits, índices de simetria, valores por peso e relações calculados pelo sistema "
              "a partir dos números do relatório do aparelho."]
    if calc["medidas"]:
        partes += ["", "### Pico de força / torque", "",
                   "| Grupo muscular | Condição | Direito | Esquerdo | Por peso (D / E) | Índice de simetria | Déficit | Classificação |",
                   "|---|---|---|---|---|---|---|---|"]
        for m in calc["medidas"]:
            pico, norm = m.get("pico"), m.get("normalizado")
            por_peso = f"{_fmt(norm['direito'], 2)} / {_fmt(norm['esquerdo'], 2)} {norm['unidade']}" if norm else "—"
            mais_fraco = f" ({pico['lado_mais_fraco']})" if pico and pico.get("lado_mais_fraco") else ""
            partes.append(
                f"| {_cell(m['grupo_muscular'])} | {_condition(m)} | {_fmt(m['pico_direito'], 1, ' ' + m['unidade'])} "
                f"| {_fmt(m['pico_esquerdo'], 1, ' ' + m['unidade'])} | {por_peso} "
                f"| {_fmt(pico['lsi_pct'], 1, '%') if pico else '—'} "
                f"| {_fmt(pico['deficit_pct'], 1, '%') + mais_fraco if pico else '—'} "
                f"| {pico['classificacao'] if pico else '—'} |"
            )
        extras = [m for m in calc["medidas"] if m.get("trabalho") or m.get("potencia")
                  or m.get("fadiga_direito_pct") is not None or m.get("fadiga_esquerdo_pct") is not None]
        if extras:
            partes += ["", "### Trabalho, potência e fadiga", "",
                       "| Grupo muscular | Condição | Déficit de trabalho | Déficit de potência | Fadiga D / E |",
                       "|---|---|---|---|---|"]
            for m in extras:
                partes.append(
                    f"| {_cell(m['grupo_muscular'])} | {_condition(m)} "
                    f"| {_fmt(m['trabalho']['deficit_pct'], 1, '%') if m.get('trabalho') else '—'} "
                    f"| {_fmt(m['potencia']['deficit_pct'], 1, '%') if m.get('potencia') else '—'} "
                    f"| {_fmt(m.get('fadiga_direito_pct'), 1, '%')} / {_fmt(m.get('fadiga_esquerdo_pct'), 1, '%')} |"
                )
    if calc["relacoes"]:
        partes += ["", "### Relações agonista / antagonista", "",
                   "| Relação | Condição | Direito | Esquerdo | Referência |", "|---|---|---|---|---|"]
        for r in calc["relacoes"]:
            cond = (f"{_fmt(r['velocidade_graus_s'], 0)}°/s" if r.get("velocidade_graus_s")
                    else f"{_fmt(r['angulo_graus'], 0)}°" if r.get("angulo_graus") is not None else "—")
            ref = f"≥ {_fmt(r['referencia_minima'], 2)}" if r.get("referencia_minima") else "—"
            if r["abaixo_da_referencia"]:
                ref += f" (abaixo: {', '.join(r['abaixo_da_referencia'])})"
            partes.append(f"| {r['relacao']} | {cond} | {_fmt(r['direito'], 2)} | {_fmt(r['esquerdo'], 2)} | {ref} |")
    if calc["plataforma"]:
        partes += ["", "### Plataforma de força", "",
                   "| Teste | Métrica | Valor | Esquerdo | Direito | Assimetria |", "|---|---|---|---|---|---|"]
        for p in calc["plataforma"]:
            un = f" {p['unidade']}" if p.get("unidade") else ""
            assim = "—"
            if p.get("assimetria_pct") is not None:
                assim = _fmt(p["assimetria_pct"], 1, "%") + (f" (menor: {p['lado_menor']})" if p.get("lado_menor") else "")
            partes.append(f"| {_cell(p['teste'])} | {_cell(p['metrica'])} | {_fmt(p['valor'], 1, un)} "
                          f"| {_fmt(p['esquerdo'], 1, un)} | {_fmt(p['direito'], 1, un)} | {assim} |")
    if calc.get("observacoes"):
        partes += ["", f"Observações do relatório: {_cell(calc['observacoes'])}"]
    partes += ["", "Referências de leitura: déficit < 10% simétrico, 10–15% leve, 15–20% moderado, ≥ 20% importante; "
               "índice de simetria ≥ 90% é o critério mais usado para retorno ao esporte. As relações de referência "
               "variam com a velocidade e o protocolo."]
    return "\n".join(partes)


def build_interpretation_prompt(calc: dict, description: str) -> str:
    resumo = json.dumps(calc, ensure_ascii=False, default=str)[:12000]
    return f"""Você é especialista em avaliação funcional e reabilitação musculoesquelética da plataforma Three Health.
Abaixo estão os resultados de um exame de dinamometria. Os valores derivados (índice de simetria, déficit,
classificação, valores por peso, relações agonista/antagonista e assimetrias) JÁ FORAM CALCULADOS pelo
sistema. NÃO recalcule nem altere nenhum número: cite-os como estão.

HISTÓRICO CLÍNICO (pode estar vazio): {description or "Não informado"}

RESULTADOS (JSON): {resumo}

Escreva SOMENTE as seções abaixo, com estes títulos exatos, em português, sem introdução:

## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
Liste os achados relevantes. Use **[FORTE]** para déficits ≥ 15%, relações claramente fora da referência ou
assimetrias ≥ 15%; **[MODERADO]** para valores limítrofes (déficit 10–15%, relação próxima ao limite) ou
quando o protocolo limita a leitura. Cite grupo muscular, condição (velocidade/ângulo), lado e o número.
Termine com "> **Achados descartados por evidência insuficiente:** ..." (ou "Nenhum achado descartado.").

## 4. IMPRESSÃO FUNCIONAL
Síntese do perfil de força: simetria, equilíbrio agonista/antagonista, resistência à fadiga, potência.

## 5. IMPACTO FUNCIONAL E RISCO
O que os déficits significam para a função e para o risco de lesão ou relesão, correlacionando com o
histórico clínico quando houver.

## 6. CONDUTA E PRÓXIMOS PASSOS
Prioridades de treino/reabilitação por grupo muscular, critérios de retorno (ex.: índice de simetria ≥ 90%)
e quando repetir o teste. Sem prescrever medicamentos.

Seja objetivo (até 700 palavras). Se algum dado essencial faltar (ex.: massa corporal, lado acometido),
diga o que mudaria na interpretação."""


def compose_report(calc: dict, interpretation: str | None) -> str:
    partes = [format_identification(calc), "", format_results(calc), ""]
    texto = (interpretation or "").strip()
    if texto:
        inicio = texto.find("## 3.")
        partes.append(texto[inicio:] if inicio >= 0 else texto)
    else:
        partes.append("## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA\n"
                      "> Interpretação automática indisponível no momento. Os valores e cálculos acima "
                      "estão completos; revise-os diretamente.")
    return "\n".join(partes).strip()
