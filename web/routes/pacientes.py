"""Prontuário dos pacientes: cadastro, linha do tempo, tratamentos e evolução.

Cada profissional só enxerga os próprios pacientes. Diferente do histórico de
laudos, não há visão de admin sobre os pacientes dos outros: o prontuário tem
nome e CPF. O admin tem conta própria (accounts.admin_cliente_id) e vê só os
pacientes que ele mesmo cadastrou.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from core import acompanhamento, analyzer, comparacao, db, idioma, patient_crypto, privacy
from web import config, costs, posthog, prontuario
from web.extensions import rate_limit
from web.security import premium_required

logger = logging.getLogger(__name__)

bp = Blueprint("pacientes", __name__)

_UNAVAILABLE = (
    "O prontuário está indisponível no momento (banco de dados ou chave de proteção "
    "dos dados do paciente não configurados). Fale com o suporte."
)
_DUPLICATED = "Já existe um paciente com este CPF no seu cadastro."
# Laudos que não pedem plano de reabilitação (ver core/analyzer.build_analysis_prompt).
_SEM_PLANO = {"correlacao", "dinamometria", "laboratorial"}


def _professional_id() -> str | None:
    """cliente_id de quem está logado (o admin usa a conta própria) — acesso local não tem pacientes."""
    return g.get("cliente_id")


def _require_professional():
    cliente_id = _professional_id()
    if not cliente_id:
        flash("O prontuário de pacientes está disponível para contas de profissional.", "error")
        return None, redirect(url_for("pacientes.lista"))
    return cliente_id, None


def _own_patient_or_404(paciente_id: str) -> tuple[str, dict]:
    cliente_id = _professional_id()
    paciente = db.buscar_paciente(paciente_id, cliente_id) if cliente_id else None
    if not paciente:
        abort(404)
    return cliente_id, paciente


def _capture(event: str) -> None:
    # Sem nome, CPF ou id de paciente: só o tipo de ação.
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture(event)


@bp.route("/pacientes")
@premium_required
def lista():
    g.modo = "premium"
    cliente_id = _professional_id()
    pacientes = db.listar_pacientes(cliente_id) if cliente_id else []
    busca = (request.args.get("q") or "").strip()
    if busca:
        termo = busca.casefold()
        digitos = patient_crypto.cpf_digits(busca)
        pacientes = [
            p for p in pacientes
            if termo in p["nome"].casefold() or (digitos and digitos in patient_crypto.cpf_digits(p["cpf"]))
        ]
    for p in pacientes:
        p["idade"] = prontuario.idade(p.get("data_nascimento"))
    return render_template(
        "pacientes.html",
        pacientes=pacientes,
        busca=busca,
        has_account=cliente_id is not None,
    )


@bp.route("/pacientes/novo", methods=["GET", "POST"])
@premium_required
@rate_limit("PATIENT_WRITE_RATE_LIMIT", "120 per hour", methods=["POST"])
def novo():
    g.modo = "premium"
    cliente_id, redirect_resp = _require_professional()
    if redirect_resp:
        return redirect_resp

    if request.method == "GET":
        return render_template("paciente_form.html", paciente={}, editing=False, sexos=prontuario.SEXOS)

    fields, errors = prontuario.paciente_from_form(request.form, require_consent=True)
    if errors:
        for message in errors:
            flash(message, "error")
        return render_template("paciente_form.html", paciente=fields, editing=False, sexos=prontuario.SEXOS), 400

    paciente_id, erro = db.criar_paciente(cliente_id=cliente_id, **fields)
    if erro:
        flash(_DUPLICATED if erro == db.PACIENTE_DUPLICADO else _UNAVAILABLE, "error")
        status = 409 if erro == db.PACIENTE_DUPLICADO else 503
        return render_template("paciente_form.html", paciente=fields, editing=False, sexos=prontuario.SEXOS), status

    db.registrar_acesso_prontuario(cliente_id, paciente_id, "criar")
    _capture("patient_created")
    flash("Paciente cadastrado.", "success")
    return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id))


@bp.route("/pacientes/<paciente_id>")
@premium_required
def detalhe(paciente_id: str):
    g.modo = "premium"
    cliente_id, paciente = _own_patient_or_404(paciente_id)
    analises = db.listar_analises_paciente(paciente_id, cliente_id)
    tratamentos = db.listar_tratamentos(paciente_id, cliente_id)
    evolucoes = db.listar_evolucoes(paciente_id, cliente_id)
    db.registrar_acesso_prontuario(cliente_id, paciente_id, "visualizar")

    # "Montar plano a partir do laudo": só pré-preenche o formulário de tratamento;
    # nada é gravado até o profissional revisar e salvar.
    plano = None
    plano_de = (request.args.get("plano_de") or "").strip()
    if plano_de:
        if any(a.get("id") == plano_de for a in analises):
            analise = db.buscar_analise(plano_de, cliente_id)
            plano = prontuario.plano_sugerido(analise) if analise else None
        if plano is None:
            flash("Este laudo não traz um plano de reabilitação (laudos anteriores a essa função, "
                  "dinamometria e exames laboratoriais não têm). Preencha o tratamento à mão.", "error")

    metas = acompanhamento.avaliar_todas(
        db.listar_metas(cliente_id, paciente_id), db.listar_dados_estruturados_pacientes(cliente_id, paciente_id)
    )
    return render_template(
        "paciente_detalhe.html",
        metas=metas,
        operadores=acompanhamento.OPERADORES,
        itens_meta=acompanhamento.itens_sugeridos(db.listar_dados_estruturados_pacientes(cliente_id, paciente_id)),
        lembretes=db.listar_lembretes(cliente_id, paciente_id),
        hoje=date.today(),
        paciente=paciente,
        idade=prontuario.idade(paciente.get("data_nascimento")),
        analises=analises,
        tratamentos=tratamentos,
        tratamentos_ativos=[t for t in tratamentos if not t.get("data_fim")],
        evolucoes=evolucoes,
        linha_do_tempo=prontuario.linha_do_tempo(analises, tratamentos, evolucoes),
        grafico=prontuario.grafico_evolucao(evolucoes, tratamentos, analises),
        tipos_tratamento=prontuario.TIPOS_TRATAMENTO,
        exames_correlacionaveis=sum(1 for a in analises if a.get("tipo_exame") != "correlacao"),
        plano=plano,
        laudos_com_plano=[a for a in analises if a.get("tipo_exame") not in _SEM_PLANO],
        pode_comparar=bool(comparacao.pares_disponiveis(analises)),
    )


@bp.route("/pacientes/<paciente_id>/retorno")
@premium_required
def retorno(paciente_id: str):
    """O que mudou desde a última vez: exame novo × anterior do mesmo tipo, lado a lado."""
    g.modo = "premium"
    cliente_id, paciente = _own_patient_or_404(paciente_id)
    pares = comparacao.pares_disponiveis(db.listar_analises_paciente(paciente_id, cliente_id))
    if not pares:
        flash("Para comparar, o paciente precisa de dois exames do mesmo tipo vinculados ao prontuário.", "info")
        return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id))

    tipo = request.args.get("tipo") or ""
    if tipo not in pares:
        # Tipo com o exame mais recente: é o retorno que o profissional está atendendo agora.
        tipo = max(pares, key=lambda t: pares[t][0].get("created_at") or "")
    laudos = pares[tipo]
    ids = [a["id"] for a in laudos]
    depois_id = request.args.get("depois") if request.args.get("depois") in ids else ids[0]
    antes_padrao = next(i for i in ids if i != depois_id)
    antes_id = request.args.get("antes") if request.args.get("antes") in ids else antes_padrao
    if antes_id == depois_id:
        antes_id = antes_padrao

    def _carregar(analise_id: str) -> dict:
        row = db.buscar_analise(analise_id, cliente_id) or {}
        return {
            "id": analise_id,
            "created_at": row.get("created_at"),
            "texto": row.get("analise_completa") or "",
            "dados": db.buscar_dados_estruturados(analise_id, cliente_id),
        }

    antes, depois = _carregar(antes_id), _carregar(depois_id)
    if (antes["created_at"] or "") > (depois["created_at"] or ""):
        antes, depois = depois, antes
    resultado = comparacao.comparar(tipo, antes, depois)
    db.registrar_acesso_prontuario(cliente_id, paciente_id, "comparar")
    _capture("patient_followup_compared")
    return render_template(
        "paciente_retorno.html",
        paciente=paciente,
        tipo=tipo,
        tipos=list(pares),
        laudos=laudos,
        antes=antes,
        depois=depois,
        comparacao=resultado,
    )


# ── Metas e lembretes ─────────────────────────────────────────────────────────

@bp.route("/pacientes/<paciente_id>/metas", methods=["POST"])
@premium_required
@rate_limit("PATIENT_WRITE_RATE_LIMIT", "120 per hour")
def nova_meta(paciente_id: str):
    g.modo = "premium"
    cliente_id, _paciente = _own_patient_or_404(paciente_id)
    voltar = redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#metas")
    campos, errors = prontuario.meta_from_form(request.form)
    if errors:
        for message in errors:
            flash(message, "error")
        return voltar
    if not db.salvar_meta(paciente_id, cliente_id, campos):
        flash("Não foi possível salvar a meta (o banco de dados foi atualizado?).", "error")
        return voltar
    db.registrar_acesso_prontuario(cliente_id, paciente_id, "meta")
    _capture("patient_goal_created")
    flash("Meta registrada. O progresso é conferido a cada exame novo.", "success")
    return voltar


@bp.route("/pacientes/<paciente_id>/metas/<meta_id>/<acao>", methods=["POST"])
@premium_required
def alterar_meta(paciente_id: str, meta_id: str, acao: str):
    g.modo = "premium"
    cliente_id, _paciente = _own_patient_or_404(paciente_id)
    if acao == "excluir":
        ok = db.excluir_meta(meta_id, paciente_id, cliente_id)
    elif acao in {"concluir", "reabrir"}:
        valor = datetime.now(timezone.utc).isoformat() if acao == "concluir" else None
        ok = db.atualizar_meta(meta_id, paciente_id, cliente_id, {"concluida_em": valor})
    else:
        abort(404)
    if not ok:
        flash("Não foi possível alterar a meta.", "error")
    return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#metas")


@bp.route("/pacientes/<paciente_id>/lembretes", methods=["POST"])
@premium_required
@rate_limit("PATIENT_WRITE_RATE_LIMIT", "120 per hour")
def novo_lembrete(paciente_id: str):
    g.modo = "premium"
    cliente_id, _paciente = _own_patient_or_404(paciente_id)
    voltar = redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#lembretes")
    texto = " ".join((request.form.get("texto") or "").split())[:300]
    data = acompanhamento.data_do_lembrete(request.form)
    if len(texto) < 3:
        flash("Descreva o lembrete (ex.: repetir CK).", "error")
        return voltar
    if data is None:
        flash("Escolha a data ou em quantos dias/semanas/meses.", "error")
        return voltar
    if not db.salvar_lembrete(cliente_id, paciente_id, texto, data.isoformat()):
        flash("Não foi possível salvar o lembrete (o banco de dados foi atualizado?).", "error")
        return voltar
    _capture("reminder_created")
    flash(f"Lembrete marcado para {data.strftime('%d/%m/%Y')}.", "success")
    return voltar


@bp.route("/lembretes/<lembrete_id>/concluir", methods=["POST"])
@premium_required
def concluir_lembrete(lembrete_id: str):
    g.modo = "premium"
    cliente_id, redirect_resp = _require_professional()
    if redirect_resp:
        return redirect_resp
    concluido = request.form.get("desfazer") != "1"
    if not db.concluir_lembrete(lembrete_id, cliente_id, concluido=concluido):
        flash("Não foi possível atualizar o lembrete.", "error")
    destino = request.form.get("voltar") or ""
    # Só caminhos internos: nada de redirecionar para outro site.
    if not destino.startswith("/") or destino.startswith("//"):
        destino = url_for("pacientes.hoje")
    return redirect(destino)


@bp.route("/hoje")
@premium_required
def hoje():
    """Tela do dia: lembretes vencendo, metas que o último exame moveu e laudos a revisar."""
    g.modo = "premium"
    cliente_id = _professional_id()
    if not cliente_id:
        return redirect(url_for("pacientes.lista"))
    pacientes = {p["id"]: p for p in db.listar_pacientes(cliente_id)}
    lembretes = acompanhamento.separar_lembretes(db.listar_lembretes(cliente_id))
    for grupo in lembretes.values():
        for item in grupo:
            item["paciente"] = pacientes.get(item.get("paciente_id"))

    dados_por_paciente: dict[str, list[dict]] = {}
    for analise in db.listar_dados_estruturados_pacientes(cliente_id):
        dados_por_paciente.setdefault(analise["paciente_id"], []).append(analise)
    metas_por_paciente: dict[str, list[dict]] = {}
    for meta in db.listar_metas(cliente_id):
        metas_por_paciente.setdefault(meta["paciente_id"], []).append(meta)
    alertas = []
    for pid, metas in metas_por_paciente.items():
        for meta in acompanhamento.alertas_das_metas(
            acompanhamento.avaliar_todas(metas, dados_por_paciente.get(pid, []))
        ):
            alertas.append({**meta, "paciente": pacientes.get(pid)})

    desde = (date.today() - timedelta(days=7)).isoformat()
    recentes = [a for a in db.listar_analises(cliente_id=cliente_id, limit=30) if (a.get("created_at") or "")[:10] >= desde]
    validados = {v.get("analise_id") for v in db.listar_validacoes_clinicas([a["id"] for a in recentes])}
    for a in recentes:
        a["paciente"] = pacientes.get(a.get("paciente_id"))
    return render_template(
        "hoje.html",
        lembretes=lembretes,
        alertas=alertas,
        a_revisar=[a for a in recentes if a["id"] not in validados][:10],
        total_pacientes=len(pacientes),
        operadores=acompanhamento.OPERADORES,
    )


@bp.route("/busca")
@premium_required
def busca():
    """Um campo só: acha paciente (nome/CPF), laudo e achado ("menisco", "LDL") em tudo."""
    g.modo = "premium"
    cliente_id = _professional_id()
    termo = " ".join((request.args.get("q") or "").split())[:80]
    pacientes, laudos = [], []
    if cliente_id and len(termo) >= 2:
        todos = db.listar_pacientes(cliente_id)
        por_id = {p["id"]: p for p in todos}
        chave, digitos = termo.casefold(), patient_crypto.cpf_digits(termo)
        pacientes = [
            p for p in todos
            if chave in p["nome"].casefold() or (len(digitos) >= 3 and digitos in patient_crypto.cpf_digits(p["cpf"]))
        ][:20]
        for row in db.buscar_laudos_por_texto(cliente_id, termo):
            row["paciente"] = por_id.get(row.get("paciente_id"))
            row["trechos"] = prontuario.trechos(row.pop("analise_completa", "") or "", termo)
            laudos.append(row)
        _capture("search_used")
    return render_template("busca.html", termo=termo, pacientes=pacientes, laudos=laudos,
                           has_account=cliente_id is not None)


@bp.route("/pacientes/<paciente_id>/exportar.csv")
@premium_required
def exportar_csv(paciente_id: str):
    """Prontuário do paciente em CSV (LGPD, troca de clínica): cadastro, exames, valores, tratamentos, evolução, metas."""
    import csv
    import io

    from flask import Response

    cliente_id, paciente = _own_patient_or_404(paciente_id)
    analises = db.listar_analises_paciente(paciente_id, cliente_id)
    dados = db.listar_dados_estruturados_pacientes(cliente_id, paciente_id)
    linhas = [("categoria", "data", "item", "valor", "unidade", "detalhe")]
    linhas.append(("paciente", "", "nome", paciente.get("nome", ""), "", ""))
    linhas.append(("paciente", "", "cpf", paciente.get("cpf", ""), "", ""))
    linhas.append(("paciente", paciente.get("data_nascimento") or "", "nascimento", "", "", paciente.get("sexo") or ""))
    for a in analises:
        linhas.append(("laudo", (a.get("created_at") or "")[:10], a.get("tipo_exame") or "", "", "", a.get("id")))
    for v in acompanhamento.medicoes(dados):
        linhas.append(("valor", v["data"].isoformat() if v.get("data") else "", v["item"], v["valor"], v["unidade"], v["analise_id"]))
    for t in db.listar_tratamentos(paciente_id, cliente_id):
        linhas.append(("tratamento", t.get("data_inicio") or "", t.get("tipo") or "", "", "",
                       f"{t.get('regiao') or ''} {t.get('descricao') or ''} até {t.get('data_fim') or 'em andamento'}".strip()))
    for e in db.listar_evolucoes(paciente_id, cliente_id):
        linhas.append(("evolucao", e.get("data") or "", "dor (EVA)", e.get("dor_eva") if e.get("dor_eva") is not None else "", "0-10", e.get("nota") or ""))
        if e.get("funcao") is not None:
            linhas.append(("evolucao", e.get("data") or "", "função", e["funcao"], "0-100", ""))
    for m in acompanhamento.avaliar_todas(db.listar_metas(cliente_id, paciente_id), dados):
        linhas.append(("meta", m.get("prazo") or "", m["descricao"], (m.get("ultimo") or {}).get("valor", ""), "", m["status"]))

    buf = io.StringIO()
    # ; e BOM: o Excel em português abre direto, com acentos.
    writer = csv.writer(buf, delimiter=";")
    for linha in linhas:
        writer.writerow(["'" + str(c) if str(c)[:1] in {"=", "+", "-", "@"} and not _numero(c) else c for c in linha])
    db.registrar_acesso_prontuario(cliente_id, paciente_id, "exportar")
    _capture("patient_exported")
    return Response(
        "\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=prontuario-{paciente_id[:8]}.csv",
                 "Cache-Control": "no-store"},
    )


def _numero(valor) -> bool:
    if isinstance(valor, (int, float)):
        return True
    try:
        float(str(valor).replace(",", "."))
        return True
    except ValueError:
        return False


@bp.route("/cron/lembretes")
def cron_lembretes():
    """E-mail diário: "você tem N reavaliações". Sem nome de paciente nem texto do lembrete.

    Chamado pelo cron da Vercel com `Authorization: Bearer <CRON_SECRET>`.
    """
    import hmac

    from core import mailer

    segredo = os.environ.get("CRON_SECRET", "").strip()
    if not segredo:
        return jsonify({"error": "CRON_SECRET não configurado."}), 503
    recebido = request.headers.get("Authorization", "")
    if not hmac.compare_digest(recebido.encode("utf-8"), f"Bearer {segredo}".encode("utf-8")):
        return jsonify({"error": "Unauthorized"}), 401
    if not mailer.is_configured():
        return jsonify({"enviados": 0, "motivo": "smtp_desligado"})

    por_cliente: dict[str, dict] = {}
    for item in db.listar_lembretes_para_email(date.today().isoformat()):
        grupo = por_cliente.setdefault(item["cliente_id"], {"email": item["email"], "nome": item["nome"], "ids": []})
        grupo["ids"].append(item["id"])

    base_url = (os.environ.get("APP_BASE_URL") or request.host_url).rstrip("/")
    enviados = 0
    for grupo in por_cliente.values():
        n = len(grupo["ids"])
        primeiro_nome = (grupo["nome"] or "").split(" ")[0]
        assunto = f"{n} reavaliaç{'ão' if n == 1 else 'ões'} para hoje — Three Health"
        corpo = (
            f"Olá{', ' + primeiro_nome if primeiro_nome else ''}!\n\n"
            f"Você tem {n} lembrete{'s' if n != 1 else ''} de reavaliação vencendo hoje ou atrasado{'s' if n != 1 else ''}.\n"
            f"Veja quais são em {base_url}/hoje\n\n"
            "Por segurança, este e-mail não traz o nome dos pacientes."
        )
        if mailer.send_email(grupo["email"], assunto, corpo):
            db.marcar_lembretes_email(grupo["ids"])
            enviados += 1
    return jsonify({"enviados": enviados, "profissionais": len(por_cliente)})


@bp.route("/pacientes/<paciente_id>/contexto")
@premium_required
def contexto(paciente_id: str):
    """Contexto clínico sem identificação, para pré-preencher a nova análise."""
    cliente_id, paciente = _own_patient_or_404(paciente_id)
    return jsonify({
        "idade": prontuario.idade(paciente.get("data_nascimento")),
        "sexo": paciente.get("sexo") if paciente.get("sexo") in {"Masculino", "Feminino"} else "",
        "tratamentos": prontuario.resumo_tratamentos(db.listar_tratamentos(paciente_id, cliente_id)),
    })


@bp.route("/pacientes/<paciente_id>/editar", methods=["GET", "POST"])
@premium_required
@rate_limit("PATIENT_WRITE_RATE_LIMIT", "120 per hour", methods=["POST"])
def editar(paciente_id: str):
    g.modo = "premium"
    cliente_id, paciente = _own_patient_or_404(paciente_id)

    if request.method == "GET":
        return render_template("paciente_form.html", paciente=paciente, editing=True, sexos=prontuario.SEXOS)

    fields, errors = prontuario.paciente_from_form(request.form, require_consent=False)
    fields["id"] = paciente_id
    if errors:
        for message in errors:
            flash(message, "error")
        return render_template("paciente_form.html", paciente=fields, editing=True, sexos=prontuario.SEXOS), 400

    erro = db.atualizar_paciente(
        paciente_id, cliente_id, **{k: v for k, v in fields.items() if k != "id"}
    )
    if erro:
        flash(_DUPLICATED if erro == db.PACIENTE_DUPLICADO else _UNAVAILABLE, "error")
        status = 409 if erro == db.PACIENTE_DUPLICADO else 503
        return render_template("paciente_form.html", paciente=fields, editing=True, sexos=prontuario.SEXOS), status

    db.registrar_acesso_prontuario(cliente_id, paciente_id, "editar")
    flash("Cadastro atualizado.", "success")
    return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id))


@bp.route("/pacientes/<paciente_id>/excluir", methods=["POST"])
@premium_required
def excluir(paciente_id: str):
    g.modo = "premium"
    cliente_id, paciente = _own_patient_or_404(paciente_id)
    if (request.form.get("confirmacao") or "").strip().upper() != "EXCLUIR":
        flash("Para excluir, digite EXCLUIR no campo de confirmação.", "error")
        return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id))
    if not db.excluir_paciente(paciente_id, cliente_id):
        flash("Não foi possível excluir o paciente agora.", "error")
        return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id))
    db.registrar_acesso_prontuario(cliente_id, None, "excluir")
    _capture("patient_deleted")
    flash("Paciente excluído. Os laudos dele continuam no histórico, sem vínculo.", "success")
    return redirect(url_for("pacientes.lista"))


@bp.route("/pacientes/<paciente_id>/tratamentos", methods=["POST"])
@premium_required
@rate_limit("PATIENT_WRITE_RATE_LIMIT", "120 per hour")
def novo_tratamento(paciente_id: str):
    g.modo = "premium"
    cliente_id, _ = _own_patient_or_404(paciente_id)
    fields, errors = prontuario.tratamento_from_form(request.form)
    if errors:
        for message in errors:
            flash(message, "error")
    elif db.salvar_tratamento(paciente_id=paciente_id, cliente_id=cliente_id, **fields):
        db.registrar_acesso_prontuario(cliente_id, paciente_id, "tratamento")
        _capture("patient_treatment_added")
        flash("Tratamento registrado.", "success")
    else:
        flash("Não foi possível registrar o tratamento agora.", "error")
    return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#tratamentos")


@bp.route("/pacientes/<paciente_id>/tratamentos/<tratamento_id>/encerrar", methods=["POST"])
@premium_required
def encerrar_tratamento(paciente_id: str, tratamento_id: str):
    g.modo = "premium"
    cliente_id, _ = _own_patient_or_404(paciente_id)
    data_fim = prontuario.parse_date(request.form.get("data_fim"))
    if data_fim is None:
        flash("Informe a data de término do tratamento.", "error")
    elif db.encerrar_tratamento(tratamento_id, paciente_id, cliente_id, data_fim.isoformat()):
        db.registrar_acesso_prontuario(cliente_id, paciente_id, "tratamento")
        flash("Tratamento encerrado.", "success")
    else:
        flash("Não foi possível encerrar o tratamento agora.", "error")
    return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#tratamentos")


@bp.route("/pacientes/<paciente_id>/evolucoes", methods=["POST"])
@premium_required
@rate_limit("PATIENT_WRITE_RATE_LIMIT", "120 per hour")
def nova_evolucao(paciente_id: str):
    g.modo = "premium"
    cliente_id, _ = _own_patient_or_404(paciente_id)
    fields, errors = prontuario.evolucao_from_form(request.form)
    if errors:
        for message in errors:
            flash(message, "error")
    elif db.salvar_evolucao(paciente_id=paciente_id, cliente_id=cliente_id, **fields):
        db.registrar_acesso_prontuario(cliente_id, paciente_id, "evolucao")
        _capture("patient_progress_added")
        flash("Evolução registrada.", "success")
    else:
        flash("Não foi possível registrar a evolução agora.", "error")
    return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#evolucao")


@bp.route("/pacientes/<paciente_id>/correlacionar", methods=["POST"])
@premium_required
@rate_limit("CORRELATION_RATE_LIMIT", "20 per hour")
def correlacionar(paciente_id: str):
    """Parecer que correlaciona os exames do paciente (imagem, dinamometria, laboratoriais)."""
    g.modo = "premium"
    cliente_id, paciente = _own_patient_or_404(paciente_id)
    voltar = redirect(url_for("pacientes.detalhe", paciente_id=paciente_id) + "#correlacao")

    exames = db.listar_laudos_para_correlacao(paciente_id, cliente_id)
    if len(exames) < 2:
        flash("A correlação precisa de pelo menos dois exames vinculados a este paciente.", "error")
        return voltar
    if not config.has_ai_provider():
        flash("Serviço de análise temporariamente indisponível. Tente novamente mais tarde.", "error")
        return voltar
    if costs.blocked_by_cost_budget("premium"):
        flash("Limite de gasto diário atingido. Tente novamente mais tarde.", "error")
        return voltar

    def _sem_identificacao(texto):
        return privacy.redact_identifiers(texto or "")[0] or None

    tratamentos = [{**t, "descricao": _sem_identificacao(t.get("descricao"))}
                   for t in db.listar_tratamentos(paciente_id, cliente_id)]
    evolucoes = [{**e, "nota": _sem_identificacao(e.get("nota"))}
                 for e in db.listar_evolucoes(paciente_id, cliente_id)]
    perfil = {"idade": prontuario.idade(paciente.get("data_nascimento")), "sexo": paciente.get("sexo")}
    laudo_idioma = idioma.normalizar(request.form.get("laudo_idioma"))
    try:
        resultado = analyzer.correlate_patient_exams(
            exames, tratamentos, evolucoes, perfil,
            api_key=config.get_api_key(), anthropic_api_key=config.get_anthropic_api_key(),
            model_name=config.get_model_name(), posthog_client=posthog.init_posthog(),
            posthog_distinct_id=str(cliente_id), laudo_idioma=laudo_idioma,
        )
    except Exception:
        logger.exception("Falha na correlação de exames")
        flash("Não foi possível gerar a correlação agora. Tente novamente em instantes.", "error")
        return voltar
    costs.record_analysis_cost("premium", resultado)

    analise_id = db.salvar_analise(
        tipo_exame="correlacao",
        analise_completa=resultado["analysis"],
        modelo_ia=resultado["model_used"],
        referencias_usadas=0,
        num_imagens=0,
        modo="premium",
        descricao_usuario=f"Correlação de {len(exames)} exames do prontuário",
        modalidade="Correlação",
        cliente_id=cliente_id,
        paciente_id=paciente_id,
        idioma=resultado.get("idioma"),
    )
    if not analise_id:
        flash("A correlação foi gerada, mas não pôde ser salva. Tente novamente.", "error")
        return voltar
    db.registrar_acesso_prontuario(cliente_id, paciente_id, "correlacionar")
    _capture("patient_exams_correlated")
    flash("Correlação dos exames pronta e salva no prontuário.", "success")
    return redirect(url_for("laudos.laudo_detalhe", analise_id=analise_id))


@bp.route("/laudos/<analise_id>/paciente", methods=["POST"])
@premium_required
def vincular_laudo(analise_id: str):
    """Liga um laudo já salvo a um paciente (ou desfaz o vínculo com paciente_id vazio)."""
    g.modo = "premium"
    cliente_id, redirect_resp = _require_professional()
    if redirect_resp:
        return redirect_resp
    paciente_id = (request.form.get("paciente_id") or "").strip() or None
    if paciente_id and not db.buscar_paciente(paciente_id, cliente_id):
        flash("Paciente não encontrado no seu cadastro.", "error")
        return redirect(url_for("laudos.laudo_detalhe", analise_id=analise_id))
    if not db.vincular_analise_paciente(analise_id, paciente_id, cliente_id):
        flash("Laudo não encontrado para este acesso.", "error")
        return redirect(url_for("laudos.laudos"))
    db.registrar_acesso_prontuario(cliente_id, paciente_id, "vincular_laudo" if paciente_id else "desvincular_laudo")
    flash("Laudo vinculado ao prontuário." if paciente_id else "Vínculo com o paciente removido.", "success")
    if paciente_id:
        return redirect(url_for("pacientes.detalhe", paciente_id=paciente_id))
    return redirect(url_for("laudos.laudo_detalhe", analise_id=analise_id))
