"""Prontuário dos pacientes: cadastro, linha do tempo, tratamentos e evolução.

Cada profissional só enxerga os próprios pacientes. Diferente do histórico de
laudos, não há visão de admin sobre os pacientes dos outros: o prontuário tem
nome e CPF. O admin tem conta própria (accounts.admin_cliente_id) e vê só os
pacientes que ele mesmo cadastrou.
"""

import logging

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from core import analyzer, db, patient_crypto, privacy
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

    return render_template(
        "paciente_detalhe.html",
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
    )


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
    try:
        resultado = analyzer.correlate_patient_exams(
            exames, tratamentos, evolucoes, perfil,
            api_key=config.get_api_key(), anthropic_api_key=config.get_anthropic_api_key(),
            model_name=config.get_model_name(), posthog_client=posthog.init_posthog(),
            posthog_distinct_id=str(cliente_id),
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
