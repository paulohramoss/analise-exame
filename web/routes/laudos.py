"""Histórico de laudos, dashboard da clínica, feedback e validação clínica."""

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from core import annotations, cost_tracking, db, patient_report
from web import costs, posthog, presentation
from web.extensions import rate_limit
from web.security import premium_required

bp = Blueprint("laudos", __name__)

_FEEDBACK_TYPES = {
    "agree": {"tipo": "validacao", "label": "Concordo totalmente", "concordancia": True, "grau": 100},
    "partial": {"tipo": "correcao", "label": "Concordo em parte", "concordancia": False, "grau": 60},
    "disagree": {"tipo": "correcao", "label": "Discordo", "concordancia": False, "grau": 0},
}


def _find_own_analysis(analise_id: str) -> dict | None:
    """Laudo do cliente atual (admin enxerga todos)."""
    return db.buscar_analise(
        analise_id=analise_id,
        cliente_id=g.get("cliente_id"),
        include_all=bool(g.get("is_admin")),
    )


@bp.route("/laudos")
@premium_required
def laudos():
    """Lista laudos salvos do usuário premium atual."""
    g.modo = "premium"
    include_all = bool(g.get("is_admin"))
    rows = db.listar_analises(cliente_id=g.get("cliente_id"), limit=80, include_all=include_all)
    return render_template("laudos.html", analyses=rows, is_admin=include_all)


@bp.route("/laudos/<analise_id>")
@premium_required
def laudo_detalhe(analise_id: str):
    """Abre um laudo salvo no histórico."""
    g.modo = "premium"
    row = _find_own_analysis(analise_id)
    if not row:
        flash("Laudo não encontrado no histórico deste acesso.", "error")
        return redirect(url_for("laudos.laudos"))

    return render_template(
        "laudo_detalhe.html",
        laudo=row,
        analysis=row.get("analise_completa") or "",
        analysis_id=row.get("id"),
        exam_type=presentation.format_exam_label(row.get("tipo_exame")),
        user_description=row.get("descricao_usuario") or "",
        responsible=presentation.responsible_info_from_row(row),
        consensus=presentation.build_consensus_info(row.get("modelo_ia") or "", row.get("analise_completa") or ""),
        annotations=annotations.sanitize_stored(row.get("marcacoes"), row.get("num_imagens")),
        can_patient_report=bool(g.get("cliente_id")) and row.get("cliente_id") == g.get("cliente_id"),
        **_patient_link_context(row),
    )


def _patient_link_context(row: dict) -> dict:
    """Paciente do laudo e opções para vincular — só para o dono do laudo (admin, só nos próprios)."""
    cliente_id = g.get("cliente_id")
    if not cliente_id or row.get("cliente_id") != cliente_id:
        return {"paciente": None, "pacientes": [], "can_link_patient": False}
    paciente = db.buscar_paciente(row["paciente_id"], cliente_id) if row.get("paciente_id") else None
    return {
        "paciente": paciente,
        "pacientes": [] if paciente else db.listar_pacientes(cliente_id),
        "can_link_patient": True,
    }


def _own_report_analysis(analise_id: str) -> tuple[str | None, dict | None]:
    """(cliente_id, laudo) só se o laudo é da conta logada — o relatório leva a marca dela.

    Nem o admin gera relatório de laudo alheio: sairia com o logo errado.
    """
    cliente_id = g.get("cliente_id")
    row = db.buscar_analise(analise_id, cliente_id=cliente_id) if cliente_id else None
    return cliente_id, row


@bp.route("/laudos/<analise_id>/paciente")
@premium_required
def relatorio_paciente(analise_id: str):
    """Relatório do laudo em linguagem simples, com a marca da clínica, para imprimir."""
    g.modo = "premium"
    cliente_id, row = _own_report_analysis(analise_id)
    if not row:
        flash("Laudo não encontrado no histórico deste acesso.", "error")
        return redirect(url_for("laudos.laudos"))

    texto = db.buscar_relatorio_paciente(analise_id, cliente_id)
    paciente = db.buscar_paciente(row["paciente_id"], cliente_id) if row.get("paciente_id") else None
    return render_template(
        "relatorio_paciente.html",
        analysis_id=analise_id,
        relatorio=patient_report.to_html(texto) if texto else None,
        marca=db.buscar_marca_clinica(cliente_id),
        paciente_nome=(paciente or {}).get("nome") or "",
        exam_type=presentation.format_exam_label(row.get("tipo_exame")),
        exam_date=row.get("created_at"),
        responsible=presentation.responsible_info_from_row(row),
    )


@bp.route("/laudos/<analise_id>/paciente/gerar", methods=["POST"])
@premium_required
@rate_limit("PATIENT_REPORT_RATE_LIMIT", "30 per hour")
def gerar_relatorio_paciente(analise_id: str):
    """Gera (ou refaz) o texto para o paciente com a IA e grava no laudo."""
    g.modo = "premium"
    cliente_id, row = _own_report_analysis(analise_id)
    if not row:
        flash("Laudo não encontrado no histórico deste acesso.", "error")
        return redirect(url_for("laudos.laudos"))
    voltar = redirect(url_for("laudos.relatorio_paciente", analise_id=analise_id))

    if costs.blocked_by_cost_budget("premium"):
        flash("Limite de gasto diário atingido. Tente novamente mais tarde.", "error")
        return voltar
    gerado = patient_report.gerar_texto(row.get("analise_completa") or "")
    if gerado is None:
        flash("Não foi possível gerar o relatório agora. Tente novamente em instantes.", "error")
        return voltar

    texto, usage = gerado
    cost_usd = cost_tracking.estimate_cost_usd(usage["model"], usage["input_tokens"], usage["output_tokens"])
    for scope, _cap in costs.cost_budget_scopes("premium"):
        cost_tracking.add_spend(scope, cost_usd)

    if not db.salvar_relatorio_paciente(analise_id, cliente_id, texto):
        flash("O relatório foi gerado, mas não pôde ser salvo. Tente novamente.", "error")
        return voltar
    posthog.track("patient_report_generated", exam_type=row.get("tipo_exame") or "")
    flash("Relatório para o paciente pronto.", "success")
    return voltar


@bp.route("/clinica/dashboard")
@premium_required
def dashboard_clinica():
    """Dashboard operacional da clínica/usuário premium."""
    g.modo = "premium"
    include_all = bool(g.get("is_admin"))
    analyses = db.listar_analises(cliente_id=g.get("cliente_id"), limit=500, include_all=include_all)
    analysis_ids = [item.get("id") for item in analyses if item.get("id")]
    validations = db.listar_validacoes_clinicas(analysis_ids)
    feedbacks = db.listar_feedbacks_por_analises(analysis_ids)
    metrics = db.montar_metricas_dashboard(analyses, validations, feedbacks)

    validated_ids = {v.get("analise_id") for v in validations if v.get("analise_id")}
    pending = [item for item in analyses if item.get("id") not in validated_ids]

    return render_template(
        "dashboard_clinica.html",
        metrics=metrics,
        analyses=analyses[:12],
        pending=pending[:12],
        validations=validations[:12],
        is_admin=include_all,
    )


@bp.route("/feedback", methods=["POST"])
@premium_required
def feedback():
    """Recebe validação/correção médica sobre um laudo gerado."""
    g.modo = "premium"
    data = request.get_json(silent=True) or request.form
    analise_id = str(data.get("analysis_id") or "").strip()
    feedback_value = str(data.get("feedback") or "").strip()
    comentario = str(data.get("comment") or "").strip()[:3000]
    responsible_name = str(data.get("responsible_name") or "").strip()[:160]
    responsible_role = str(data.get("responsible_role") or "").strip()[:80]

    if feedback_value not in _FEEDBACK_TYPES:
        return jsonify({"error": "Tipo de feedback inválido."}), 400
    if not analise_id:
        return jsonify({"error": "Laudo sem identificação para salvar feedback."}), 400
    is_correction = feedback_value in {"partial", "disagree"}
    if is_correction and not comentario:
        return jsonify({"error": "Descreva o que deveria ser corrigido no laudo."}), 400

    row = _find_own_analysis(analise_id)
    if not row:
        return jsonify({"error": "Laudo não encontrado para este acesso."}), 404

    meta = _FEEDBACK_TYPES[feedback_value]
    fonte_nome = " — ".join(part for part in [responsible_name, responsible_role] if part)
    if not fonte_nome:
        fonte_nome = g.get("user_email") or ("admin" if g.get("is_admin") else "")

    feedback_id = db.salvar_feedback(
        analise_id=analise_id,
        tipo=meta["tipo"],
        comentario=comentario or meta["label"],
        achado_original=row.get("analise_completa") or "",
        achado_corrigido=comentario if is_correction else "",
        secao="validacao_medica",
        fonte=presentation.feedback_source_for_role(responsible_role),
        fonte_nome=fonte_nome,
    )
    if not feedback_id:
        return jsonify({"error": "Não foi possível salvar o feedback agora."}), 503

    db.salvar_diagnostico_validado(
        analise_id=analise_id,
        tipo_exame=row.get("tipo_exame") or "",
        diagnostico_ia=row.get("analise_completa") or "",
        diagnostico_final=comentario or meta["label"],
        concordancia=meta["concordancia"],
        grau_concordancia=meta["grau"],
        validado_por=fonte_nome,
        modalidade=row.get("modalidade"),
    )
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture(
            "clinical_feedback_submitted",
            properties={"feedback_type": feedback_value, "is_correction": is_correction},
        )
    return jsonify({"success": True, "message": "Feedback médico salvo no histórico de validação."})


@bp.route("/validacao-clinica", methods=["POST"])
@premium_required
def validacao_clinica():
    """Registra validação clínica formal de um laudo salvo."""
    g.modo = "premium"
    data = request.get_json(silent=True) or request.form
    wants_json = request.is_json or "application/json" in (request.headers.get("Accept") or "")

    analise_id = str(data.get("analysis_id") or "").strip()
    final_diagnosis = str(data.get("final_diagnosis") or "").strip()[:6000]
    reviewer_name = str(data.get("reviewer_name") or "").strip()[:160]
    reviewer_register = str(data.get("reviewer_register") or "").strip()[:100]
    reviewer_role = str(data.get("reviewer_role") or "").strip()[:80]
    score = presentation.score_value(data.get("concordance_score"), 100)
    correct_findings = presentation.split_validation_lines(str(data.get("correct_findings") or ""))
    missed_findings = presentation.split_validation_lines(str(data.get("missed_findings") or ""))
    wrong_findings = presentation.split_validation_lines(str(data.get("wrong_findings") or ""))
    notes = str(data.get("validation_notes") or "").strip()[:3000]

    def _respond(message: str, status: int = 200, ok: bool = True):
        if wants_json:
            payload = {"success": ok, "message": message} if ok else {"error": message}
            return jsonify(payload), status
        flash(message, "success" if ok else "error")
        return redirect(request.referrer or url_for("laudos.laudos"))

    if not analise_id:
        return _respond("Laudo sem identificação para validação.", 400, False)
    if not final_diagnosis:
        return _respond("Informe o diagnóstico final ou parecer validado.", 400, False)

    row = _find_own_analysis(analise_id)
    if not row:
        return _respond("Laudo não encontrado para este acesso.", 404, False)

    validado_por = " — ".join(part for part in [reviewer_name, reviewer_register or reviewer_role] if part)
    if not validado_por:
        validado_por = g.get("user_email") or ("admin" if g.get("is_admin") else "Profissional responsável")

    validation_id = db.salvar_diagnostico_validado(
        analise_id=analise_id,
        tipo_exame=row.get("tipo_exame") or "",
        diagnostico_ia=row.get("analise_completa") or "",
        diagnostico_final=final_diagnosis,
        concordancia=score >= 80 and not missed_findings and not wrong_findings,
        grau_concordancia=score,
        achados_corretos=correct_findings,
        achados_perdidos=missed_findings,
        achados_incorretos=wrong_findings,
        validado_por=validado_por,
        modalidade=row.get("modalidade"),
    )
    if not validation_id:
        return _respond("Não foi possível salvar a validação clínica agora.", 503, False)

    db.salvar_feedback(
        analise_id=analise_id,
        tipo="validacao",
        comentario=notes or f"Validação clínica formal registrada com {score}% de concordância.",
        achado_original=row.get("analise_completa") or "",
        achado_corrigido=final_diagnosis,
        secao="validacao_clinica_formal",
        fonte=presentation.feedback_source_for_role(reviewer_role),
        fonte_nome=validado_por,
    )
    if posthog.posthog_client is not None:
        posthog.posthog_client.capture(
            "clinical_validation_submitted",
            properties={
                "concordance_score": score,
                "correct_findings_count": len(correct_findings),
                "missed_findings_count": len(missed_findings),
                "wrong_findings_count": len(wrong_findings),
            },
        )
    return _respond("Validação clínica formal salva com sucesso.")
