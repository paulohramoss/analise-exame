"""Histórico de laudos, dashboard da clínica, feedback e validação clínica."""

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from core import db
from web import posthog, presentation
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
        **_patient_link_context(row),
    )


def _patient_link_context(row: dict) -> dict:
    """Paciente do laudo e opções para vincular — só para o profissional dono (admin não vê prontuário)."""
    cliente_id = None if g.get("is_admin") else g.get("cliente_id")
    if not cliente_id or row.get("cliente_id") != cliente_id:
        return {"paciente": None, "pacientes": [], "can_link_patient": False}
    paciente = db.buscar_paciente(row["paciente_id"], cliente_id) if row.get("paciente_id") else None
    return {
        "paciente": paciente,
        "pacientes": [] if paciente else db.listar_pacientes(cliente_id),
        "can_link_patient": True,
    }


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
