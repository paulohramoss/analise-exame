"""Prontuário: leitura dos formulários, linha do tempo e gráfico de evolução."""

from datetime import date, datetime

from core import patient_crypto
from web import presentation

SEXOS = ("Feminino", "Masculino", "Outro")

TIPOS_TRATAMENTO = (
    "Fisioterapia",
    "Exercício / reabilitação",
    "Medicação",
    "Infiltração",
    "Órtese / imobilização",
    "Cirurgia",
    "Outro",
)


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def _parse_int(value: str | None, low: int, high: int) -> tuple[int | None, bool]:
    """(valor, ok). Vazio é válido (None); fora da faixa ou não numérico, não."""
    text = (value or "").strip()
    if not text:
        return None, True
    try:
        number = int(text)
    except ValueError:
        return None, False
    return (number, True) if low <= number <= high else (None, False)


def paciente_from_form(form, require_consent: bool) -> tuple[dict, list[str]]:
    """Campos do cadastro e a lista de erros (vazia se está tudo certo)."""
    errors = []
    nome = " ".join((form.get("nome") or "").split())[:160]
    cpf = (form.get("cpf") or "").strip()
    nascimento_raw = (form.get("data_nascimento") or "").strip()
    nascimento = parse_date(nascimento_raw)
    sexo = (form.get("sexo") or "").strip()
    observacoes = (form.get("observacoes") or "").strip()[:3000]

    if len(nome) < 3:
        errors.append("Informe o nome completo do paciente.")
    if not patient_crypto.is_valid_cpf(cpf):
        errors.append("CPF inválido. Confira os 11 dígitos.")
    if nascimento_raw and (nascimento is None or nascimento > date.today() or nascimento.year < 1900):
        errors.append("Data de nascimento inválida.")
    if sexo and sexo not in SEXOS:
        errors.append("Sexo inválido.")
    if require_consent and (form.get("consentimento") or "") != "accepted":
        errors.append("Confirme que o paciente autorizou o registro dos dados no prontuário.")

    fields = {
        "nome": nome,
        "cpf": patient_crypto.format_cpf(cpf),
        "data_nascimento": nascimento.isoformat() if nascimento else None,
        "sexo": sexo or None,
        "observacoes": observacoes,
    }
    return fields, errors


def tratamento_from_form(form) -> tuple[dict, list[str]]:
    errors = []
    tipo = (form.get("tipo") or "").strip()
    inicio = parse_date(form.get("data_inicio"))
    fim_raw = (form.get("data_fim") or "").strip()
    fim = parse_date(fim_raw)
    if tipo not in TIPOS_TRATAMENTO:
        errors.append("Escolha o tipo de tratamento.")
    if inicio is None:
        errors.append("Informe a data de início do tratamento.")
    if fim_raw and (fim is None or (inicio and fim < inicio)):
        errors.append("A data de término precisa ser igual ou posterior ao início.")
    return {
        "tipo": tipo,
        "data_inicio": inicio.isoformat() if inicio else None,
        "data_fim": fim.isoformat() if fim else None,
        "descricao": (form.get("descricao") or "").strip()[:2000],
        "regiao": (form.get("regiao") or "").strip()[:120],
    }, errors


def evolucao_from_form(form) -> tuple[dict, list[str]]:
    errors = []
    data = parse_date(form.get("data"))
    dor, dor_ok = _parse_int(form.get("dor_eva"), 0, 10)
    funcao, funcao_ok = _parse_int(form.get("funcao"), 0, 100)
    nota = (form.get("nota") or "").strip()[:3000]
    if data is None or data > date.today():
        errors.append("Informe a data da evolução (não pode ser futura).")
    if not dor_ok:
        errors.append("A dor (EVA) vai de 0 a 10.")
    if not funcao_ok:
        errors.append("O escore funcional vai de 0 a 100.")
    if dor is None and funcao is None and not nota:
        errors.append("Registre a dor, o escore funcional ou uma nota de evolução.")
    return {
        "data": data.isoformat() if data else None,
        "dor_eva": dor,
        "funcao": funcao,
        "nota": nota,
    }, errors


def idade(data_nascimento: str | None, hoje: date | None = None) -> int | None:
    nascimento = parse_date(data_nascimento)
    if nascimento is None:
        return None
    hoje = hoje or date.today()
    return hoje.year - nascimento.year - ((hoje.month, hoje.day) < (nascimento.month, nascimento.day))


def resumo_tratamentos(tratamentos: list[dict]) -> str:
    """Texto curto para o campo "Tratamentos anteriores" da análise (vai para a IA, sem identificação)."""
    partes = []
    for t in tratamentos:
        inicio = parse_date(t.get("data_inicio"))
        periodo = f"desde {inicio:%m/%Y}" if inicio else ""
        if t.get("data_fim"):
            fim = parse_date(t.get("data_fim"))
            periodo = f"{inicio:%m/%Y} a {fim:%m/%Y}" if inicio and fim else periodo
        detalhe = ", ".join(p for p in [t.get("regiao") or "", periodo] if p)
        partes.append(f"{t.get('tipo')}{f' ({detalhe})' if detalhe else ''}")
    return "; ".join(partes)[:600]


def _event_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return parse_date(value)


def linha_do_tempo(analises: list[dict], tratamentos: list[dict], evolucoes: list[dict]) -> list[dict]:
    """Exames, tratamentos e evoluções num único histórico, do mais recente para o mais antigo."""
    eventos = []
    for a in analises:
        eventos.append({
            "tipo": "laudo",
            "data": _event_date(a.get("created_at")),
            "titulo": f"Exame: {presentation.format_exam_label(a.get('tipo_exame'))}",
            "detalhe": a.get("modalidade") or "",
            "analise_id": a.get("id"),
        })
    for t in tratamentos:
        eventos.append({
            "tipo": "tratamento",
            "data": parse_date(t.get("data_inicio")),
            "titulo": f"Início do tratamento: {t.get('tipo')}",
            "detalhe": " · ".join(p for p in [t.get("regiao") or "", t.get("descricao") or ""] if p),
            "tratamento": t,
        })
        if t.get("data_fim"):
            eventos.append({
                "tipo": "tratamento_fim",
                "data": parse_date(t.get("data_fim")),
                "titulo": f"Fim do tratamento: {t.get('tipo')}",
                "detalhe": t.get("regiao") or "",
            })
    for e in evolucoes:
        medidas = []
        if e.get("dor_eva") is not None:
            medidas.append(f"Dor (EVA) {e['dor_eva']}/10")
        if e.get("funcao") is not None:
            medidas.append(f"Função {e['funcao']}/100")
        eventos.append({
            "tipo": "evolucao",
            "data": parse_date(e.get("data")),
            "titulo": "Evolução" + (f": {' · '.join(medidas)}" if medidas else ""),
            "detalhe": e.get("nota") or "",
        })
    ordem = {"laudo": 0, "tratamento": 1, "tratamento_fim": 2, "evolucao": 3}
    eventos = [e for e in eventos if e["data"] is not None]
    return sorted(eventos, key=lambda e: (e["data"], -ordem[e["tipo"]]), reverse=True)


# Área de desenho do gráfico (viewBox do SVG em templates/paciente_detalhe.html).
CHART_WIDTH, CHART_HEIGHT = 640, 220
_PAD_LEFT, _PAD_RIGHT, _PAD_TOP, _PAD_BOTTOM = 36, 36, 14, 30


def grafico_evolucao(evolucoes: list[dict], tratamentos: list[dict], analises: list[dict]) -> dict | None:
    """Coordenadas do gráfico de dor (0-10, eixo esquerdo) e função (0-100, eixo direito).

    Tratamentos e exames entram como marcas verticais. None se não há medida
    nenhuma para desenhar.
    """
    medidas = [e for e in evolucoes if parse_date(e.get("data")) and (
        e.get("dor_eva") is not None or e.get("funcao") is not None
    )]
    if not medidas:
        return None

    marcos = [(parse_date(t.get("data_inicio")), t.get("tipo") or "") for t in tratamentos]
    marcos = [(d, label) for d, label in marcos if d]
    exames = [(_event_date(a.get("created_at")), presentation.format_exam_label(a.get("tipo_exame"))) for a in analises]
    exames = [(d, label) for d, label in exames if d]

    datas = [parse_date(e["data"]) for e in medidas] + [d for d, _ in marcos] + [d for d, _ in exames]
    inicio, fim = min(datas), max(datas)
    span = max((fim - inicio).days, 1)
    plot_w = CHART_WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_h = CHART_HEIGHT - _PAD_TOP - _PAD_BOTTOM

    def x_of(d: date) -> float:
        if fim == inicio:
            return _PAD_LEFT + plot_w / 2
        return round(_PAD_LEFT + plot_w * (d - inicio).days / span, 1)

    def y_of(fraction: float) -> float:
        return round(_PAD_TOP + plot_h * (1 - fraction), 1)

    def serie(campo: str, escala: int) -> list[dict]:
        return [
            {"x": x_of(parse_date(e["data"])), "y": y_of(e[campo] / escala), "valor": e[campo], "data": parse_date(e["data"])}
            for e in medidas if e.get(campo) is not None
        ]

    dor, funcao = serie("dor_eva", 10), serie("funcao", 100)
    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "top": _PAD_TOP,
        "bottom": CHART_HEIGHT - _PAD_BOTTOM,
        "left": _PAD_LEFT,
        "right": CHART_WIDTH - _PAD_RIGHT,
        "grid": [{"y": y_of(v / 10), "dor": v, "funcao": v * 10} for v in (0, 5, 10)],
        "dor": dor,
        "dor_path": " ".join(f"{p['x']},{p['y']}" for p in dor),
        "funcao": funcao,
        "funcao_path": " ".join(f"{p['x']},{p['y']}" for p in funcao),
        "tratamentos": [{"x": x_of(d), "label": label, "data": d} for d, label in marcos],
        "exames": [{"x": x_of(d), "label": label, "data": d} for d, label in exames],
        "inicio": inicio,
        "fim": fim,
    }
