"""
Acompanhamento do paciente: metas com progresso e lembretes de reavaliação.

Uma meta numérica ("LSI quadríceps ≥ 90%", "Ferritina > 50") é conferida, sem
IA, contra os valores já lidos nos laudos do paciente (`dados_estruturados` de
laboratoriais e dinamometria). O item da meta casa com o nome do exame/medida
por palavras: "LSI quadríceps" casa com "LSI Quadríceps · concentrica · 60°/s".
Vale o valor mais recente; o anterior diz se o paciente está se aproximando ou
se afastando do alvo.

Metas livres ("retorno ao esporte em 12 semanas") têm só prazo e são marcadas
como cumpridas à mão.
"""

import re
from datetime import date, datetime, timedelta

from core.comparacao import _chave, _medida_nome, _num

OPERADORES = {">=": "≥", "<=": "≤", ">": ">", "<": "<"}
_PRAZO_PROXIMO_DIAS = 7


def _date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date() if "T" in str(value) \
            else date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def medicoes(analises: list[dict]) -> list[dict]:
    """Valores numéricos dos laudos do paciente: [{item, valor, unidade, data, analise_id}], do mais antigo ao mais novo.

    analises: [{id, created_at, tipo_exame, dados}] (dados = dados_estruturados ou None).
    """
    saida = []
    for a in sorted(analises, key=lambda x: x.get("created_at") or ""):
        dados = a.get("dados") or {}
        data = (dados.get("data_coleta") if a.get("tipo_exame") == "laboratorial" else None) or a.get("created_at")
        base = {"data": _date(data), "analise_id": a.get("id")}
        if a.get("tipo_exame") == "laboratorial":
            for r in dados.get("resultados") or []:
                if r.get("valor") is not None and r.get("exame"):
                    saida.append({**base, "item": r["exame"], "valor": r["valor"], "unidade": r.get("unidade") or ""})
            for i in dados.get("indices") or []:
                if i.get("valor") is not None:
                    saida.append({**base, "item": i["nome"], "valor": i["valor"], "unidade": i.get("unidade") or ""})
        elif a.get("tipo_exame") == "dinamometria":
            for m in dados.get("medidas") or []:
                lsi = (m.get("pico") or {}).get("lsi_pct")
                if lsi is not None:
                    saida.append({**base, "item": f"LSI {_medida_nome(m)}", "valor": lsi, "unidade": "%"})
                for lado in ("direito", "esquerdo"):
                    if m.get(f"pico_{lado}") is not None:
                        saida.append({**base, "item": f"Pico {lado} {_medida_nome(m)}",
                                      "valor": m[f"pico_{lado}"], "unidade": m.get("unidade") or ""})
            for p in dados.get("plataforma") or []:
                if p.get("assimetria_pct") is not None:
                    nome = " ".join(x for x in (p.get("teste"), p.get("metrica")) if x)
                    saida.append({**base, "item": f"Assimetria {nome}", "valor": p["assimetria_pct"], "unidade": "%"})
    return saida


def _casa(meta_item: str, item: str) -> bool:
    palavras = _chave(meta_item).split()
    alvo = f" {_chave(item)} "
    return bool(palavras) and all(f" {p} " in alvo for p in palavras)


def _satisfaz(valor: float, operador: str, alvo: float) -> bool:
    return {">=": valor >= alvo, "<=": valor <= alvo, ">": valor > alvo, "<": valor < alvo}[operador]


def avaliar(meta: dict, valores: list[dict], hoje: date | None = None) -> dict:
    """Progresso da meta: status, último valor, tendência e texto pronto para a tela."""
    hoje = hoje or date.today()
    prazo = _date(meta.get("prazo"))
    operador, alvo = meta.get("operador"), meta.get("valor")
    alvo = float(alvo) if alvo is not None else None
    un = meta.get("unidade") or ""
    descricao = meta["item"]
    if operador in OPERADORES and alvo is not None:
        descricao = f"{meta['item']} {OPERADORES[operador]} {_num(alvo)}{' ' + un if un else ''}"

    resultado = {**meta, "descricao": descricao, "prazo_data": prazo, "ultimo": None, "anterior": None,
                 "tendencia": None, "progresso_pct": None}
    if prazo:
        resultado["dias_para_prazo"] = (prazo - hoje).days

    if operador not in OPERADORES or alvo is None:
        if meta.get("concluida_em"):
            status = "atingida"
        elif prazo and prazo < hoje:
            status = "vencida"
        else:
            status = "em_andamento"
        return {**resultado, "status": status}

    casadas = [v for v in valores if _casa(meta["item"], v["item"])]
    if not casadas:
        return {**resultado, "status": "vencida" if prazo and prazo < hoje else "sem_dado"}

    ultimo = casadas[-1]
    anterior = next((v for v in reversed(casadas[:-1]) if v["analise_id"] != ultimo["analise_id"]), None)
    resultado["ultimo"] = {**ultimo, "valor_fmt": f"{_num(ultimo['valor'])}{' ' + ultimo['unidade'] if ultimo['unidade'] else ''}"}
    if anterior:
        resultado["anterior"] = {**anterior, "valor_fmt": f"{_num(anterior['valor'])}"}
        dist_antes, dist_agora = abs(alvo - anterior["valor"]), abs(alvo - ultimo["valor"])
        if _satisfaz(ultimo["valor"], operador, alvo) and not _satisfaz(anterior["valor"], operador, alvo):
            resultado["tendencia"] = "atingiu"
        elif not _satisfaz(ultimo["valor"], operador, alvo) and _satisfaz(anterior["valor"], operador, alvo):
            resultado["tendencia"] = "perdeu"
        elif dist_agora < dist_antes:
            resultado["tendencia"] = "aproximando"
        elif dist_agora > dist_antes:
            resultado["tendencia"] = "afastando"
        else:
            resultado["tendencia"] = "estavel"
        # Quanto do caminho entre o valor anterior e o alvo já foi percorrido.
        if dist_antes and not _satisfaz(anterior["valor"], operador, alvo):
            resultado["progresso_pct"] = max(0, min(100, round(100 * (dist_antes - dist_agora) / dist_antes)))

    if _satisfaz(ultimo["valor"], operador, alvo):
        status = "atingida"
    elif prazo and prazo < hoje:
        status = "vencida"
    else:
        status = "nao_atingida"
    return {**resultado, "status": status}


def avaliar_todas(metas: list[dict], analises: list[dict], hoje: date | None = None) -> list[dict]:
    valores = medicoes(analises)
    ordem = {"vencida": 0, "nao_atingida": 1, "sem_dado": 2, "em_andamento": 3, "atingida": 4}
    avaliadas = [avaliar(m, valores, hoje) for m in metas]
    return sorted(avaliadas, key=lambda m: (ordem.get(m["status"], 9), m.get("prazo") or "9999"))


def itens_sugeridos(analises: list[dict]) -> list[str]:
    """Nomes de exames/medidas do paciente, para o campo da meta (datalist)."""
    vistos: dict[str, str] = {}
    for v in medicoes(analises):
        nome = re.sub(r"\s+", " ", v["item"]).strip()
        vistos.setdefault(_chave(nome), nome)
    return sorted(vistos.values(), key=str.casefold)[:200]


def alertas_das_metas(metas_avaliadas: list[dict], hoje: date | None = None, janela_dias: int = 21) -> list[dict]:
    """Metas que o exame mais recente moveu: atingiu, perdeu ou se afastou — para a tela "Hoje"."""
    hoje = hoje or date.today()
    alertas = []
    for m in metas_avaliadas:
        data = (m.get("ultimo") or {}).get("data")
        recente = data is not None and (hoje - data).days <= janela_dias
        if m.get("tendencia") in {"atingiu", "perdeu", "afastando"} and recente:
            alertas.append(m)
        elif m["status"] == "vencida" or (
            m["status"] not in {"atingida"} and m.get("dias_para_prazo") is not None
            and 0 <= m["dias_para_prazo"] <= _PRAZO_PROXIMO_DIAS
        ):
            alertas.append(m)
    return alertas


# ── Lembretes ─────────────────────────────────────────────────────────────────

UNIDADES_PRAZO = {"dias": 1, "semanas": 7, "meses": 30}


def data_do_lembrete(form: dict, hoje: date | None = None) -> date | None:
    """Data explícita (AAAA-MM-DD) ou "em N dias/semanas/meses"."""
    hoje = hoje or date.today()
    explicita = _date(form.get("data"))
    if explicita:
        return explicita
    try:
        quantidade = int(form.get("em_quantidade") or 0)
    except (TypeError, ValueError):
        return None
    fator = UNIDADES_PRAZO.get(form.get("em_unidade") or "")
    if not fator or not 0 < quantidade <= 120:
        return None
    return hoje + timedelta(days=quantidade * fator)


def separar_lembretes(lembretes: list[dict], hoje: date | None = None) -> dict:
    hoje = hoje or date.today()
    grupos = {"atrasados": [], "hoje": [], "proximos": []}
    for lembrete in lembretes:
        if lembrete.get("concluido_em"):
            continue
        data = _date(lembrete.get("data"))
        if data is None:
            continue
        chave = "atrasados" if data < hoje else "hoje" if data == hoje else "proximos"
        grupos[chave].append({**lembrete, "data_obj": data, "dias": (data - hoje).days})
    for itens in grupos.values():
        itens.sort(key=lambda x: x["data_obj"])
    return grupos
