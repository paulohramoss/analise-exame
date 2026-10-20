"""
Testes de LGPD (minimização, exportação e exclusão), observabilidade (custos,
alerta de consenso rápido, trace_id no worker) e UX (etapas da análise,
renderização única do laudo).
"""

import io
import json
import logging
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from werkzeug.security import generate_password_hash

from core import analyzer, cost_tracking, db, jobs, privacy
from core.logging_config import JsonFormatter, TraceIdFilter, bind_trace_id, current_trace_id, unbind_trace_id
from web import config, security

ROOT = Path(__file__).resolve().parent.parent

FAKE_RESULT = {
    "success": True, "exam_type": "joelho", "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\nlaudo",
    "references_used": 0, "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [], "cache_hit": False,
}


# ── Minimização de dados no texto clínico ─────────────────────────────────────

@pytest.mark.parametrize("text,removed", [
    ("Paciente: Maria da Silva, 45 anos", "Maria da Silva"),
    ("CPF 123.456.789-09, dor lombar", "123.456.789-09"),
    ("contato (11) 98765-4321", "98765-4321"),
    ("email maria.silva@clinica.com.br", "maria.silva@clinica.com.br"),
    ("Sra. Ana Lima, DN: 12/03/1979", "12/03/1979"),
    ("CNS 898 0012 3456 7890", "898 0012 3456 7890"),
])
def test_identifiers_are_redacted(text, removed):
    redacted, kinds = privacy.redact_identifiers(text)
    assert removed not in redacted
    assert "[removido]" in redacted and kinds


@pytest.mark.parametrize("clinical", [
    "paciente: 45 anos, dor no joelho há 3 meses",
    "RM ponderada em T2, protrusão L4-L5 de 3 mm, escala de dor 7/10",
    "Paciente feminina, 38 anos, trauma em 2024, LCA",
])
def test_clinical_text_is_kept(clinical):
    assert privacy.redact_identifiers(clinical) == (clinical, [])


def test_identifiers_never_reach_the_ai_or_the_database(client, monkeypatch):
    analyze = MagicMock(return_value=dict(FAKE_RESULT))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    salvar = MagicMock(return_value="analise-1")
    monkeypatch.setattr(db, "salvar_analise_com_imagens", salvar)
    client.get(f"/admin/entrar?key={config.get_admin_key()}")

    client.post(
        "/analyze",
        data={
            "exam_images": (io.BytesIO(b"fake"), "joelho.jpg"),
            "description": "Paciente: João Pedro Souza, CPF 123.456.789-09, dor no joelho",
            "privacy_consent": "accepted",
        },
        content_type="multipart/form-data",
    )

    sent = analyze.call_args.kwargs["user_description"]
    stored = salvar.call_args.kwargs["descricao_usuario"]
    for text in (sent, stored):
        assert "João Pedro Souza" not in text and "123.456.789-09" not in text
        assert "dor no joelho" in text


# ── Exportação e exclusão (LGPD art. 18) ──────────────────────────────────────

def _login_client(client, monkeypatch, email="a@example.com", cliente_id="cliente-a"):
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": cliente_id, "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_senha_hash_cliente", lambda e: generate_password_hash("senha-certa"))
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", email, 3600))


def test_export_downloads_json_attachment(client, monkeypatch):
    _login_client(client, monkeypatch)
    monkeypatch.setattr(db, "exportar_dados_cliente", lambda cid: {"cliente": {"id": cid}, "analises": []})

    resp = client.get("/conta/dados.json")

    assert resp.status_code == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    body = json.loads(resp.get_data(as_text=True))
    assert body["cliente"]["id"] == "cliente-a" and "gerado_em" in body


@pytest.mark.parametrize("form", [
    {"senha": "senha-errada", "confirmacao": "EXCLUIR"},
    {"senha": "senha-certa", "confirmacao": "talvez"},
])
def test_delete_requires_password_and_confirmation(client, monkeypatch, form):
    _login_client(client, monkeypatch)
    excluir = MagicMock(return_value=True)
    monkeypatch.setattr(db, "excluir_dados_cliente", excluir)

    resp = client.post("/conta/excluir", data=form)

    assert resp.status_code == 302 and "/conta" in resp.headers["Location"]
    excluir.assert_not_called()


def test_delete_removes_data_and_ends_session(client, monkeypatch):
    _login_client(client, monkeypatch)
    excluir = MagicMock(return_value=True)
    monkeypatch.setattr(db, "excluir_dados_cliente", excluir)

    resp = client.post("/conta/excluir", data={"senha": "senha-certa", "confirmacao": "excluir"})

    excluir.assert_called_once_with("cliente-a")
    assert resp.headers["Location"] == "/"
    assert f"{security.PREMIUM_COOKIE}=;" in resp.headers.get("Set-Cookie", "")


def test_deleted_account_is_not_recreated_by_cookie_on_another_device(client, monkeypatch):
    """Antes, um cookie válido de conta inexistente fazia upsert do cliente de volta."""
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda email: None)
    upsert = MagicMock()
    monkeypatch.setattr(db, "upsert_cliente", upsert)
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "apagado@example.com", 3600))

    resp = client.get("/laudos")

    assert resp.status_code == 302
    upsert.assert_not_called()


def test_export_query_excludes_secrets(monkeypatch):
    selects = []

    class _Query:
        def __init__(self, table):
            self.table = table

        def select(self, columns):
            selects.append((self.table, columns))
            return self

        def __getattr__(self, name):
            return lambda *a, **k: self

        def execute(self):
            return MagicMock(data=[{"id": "x"}] if self.table == "clientes" else [])

    fake = MagicMock()
    fake.table.side_effect = _Query
    monkeypatch.setattr(db, "_get_client", lambda: fake)

    db.exportar_dados_cliente("cliente-a")

    cliente_cols = next(cols for table, cols in selects if table == "clientes")
    assert "senha_hash" not in cliente_cols and "sessao_versao" not in cliente_cols


# ── Observabilidade ───────────────────────────────────────────────────────────

def test_outcomes_feed_the_daily_report():
    cost_tracking.record_outcome("premium", "gemini-2.5-flash + claude-sonnet-4-6 → consenso", 0.05)
    cost_tracking.record_outcome("premium", "gemini-2.5-flash → consenso rápido", 0.01)
    cost_tracking.record_outcome("trial", "gemini-2.5-flash", 0.02)

    today = cost_tracking.daily_report(1)[0]

    assert today["modos"]["premium"] == {"analises": 2, "custo_usd": 0.06, "consenso_rapido": 1}
    assert today["analises"] == 3
    assert today["taxa_consenso_rapido"] == round(1 / 3, 3)


def test_fallback_alert_fires_once_per_day(monkeypatch, caplog):
    monkeypatch.setenv("CONSENSUS_FALLBACK_ALERT_RATIO", "0.5")
    monkeypatch.setenv("CONSENSUS_FALLBACK_ALERT_MIN_ANALYSES", "4")
    with caplog.at_level(logging.ERROR, logger="core.cost_tracking"):
        for _ in range(6):
            cost_tracking.record_outcome("api", "gemini-2.5-flash → consenso rápido", 0.0)

    alerts = [r for r in caplog.records if "consenso rápido" in r.getMessage()]
    assert len(alerts) == 1


def test_admin_costs_page_is_admin_only(client):
    assert client.get("/admin/custos").status_code == 403
    client.get(f"/admin/entrar?key={config.get_admin_key()}")
    resp = client.get("/admin/custos?dias=7")
    assert resp.status_code == 200
    assert "Custos e qualidade" in resp.get_data(as_text=True)


def test_trace_id_binding_outside_requests():
    assert current_trace_id() is None
    token = bind_trace_id("abc123")
    try:
        record = logging.LogRecord("x", logging.INFO, "", 0, "msg", None, None)
        TraceIdFilter().filter(record)
        assert json.loads(JsonFormatter().format(record))["trace_id"] == "abc123"
    finally:
        unbind_trace_id(token)
    assert current_trace_id() is None


def test_worker_logs_carry_the_request_trace_id(client, fake_redis_conn, run_pending_jobs, monkeypatch):
    seen = {}

    def _analyze(**kwargs):
        seen["trace_id"] = current_trace_id()
        return dict(FAKE_RESULT)

    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", _analyze)
    resp = client.post(
        "/trial/analyze",
        data={"exam_image": (io.BytesIO(b"fake"), "joelho.jpg"), "privacy_consent": "accepted"},
        content_type="multipart/form-data",
    )
    request_trace_id = resp.headers["X-Trace-Id"]

    run_pending_jobs(fake_redis_conn)

    assert seen["trace_id"] == request_trace_id


# ── Etapas da análise ─────────────────────────────────────────────────────────

def test_job_reports_stages(client, fake_redis_conn, run_pending_jobs, monkeypatch):
    def _analyze(**kwargs):
        kwargs["on_progress"]("analisando")
        kwargs["on_progress"]("consenso")
        return dict(FAKE_RESULT)

    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", _analyze)
    job_id = jobs.enqueue_analysis(
        owner=None, mode="trial", images=[(b"x", "image/jpeg")], image_meta=[{}], exam_filename="j.jpg",
        user_description="", model_name="gemini-2.5-flash", cost_scopes=[], persist={},
    )
    assert jobs.get_job_status(job_id)["stage"] == "na_fila"

    run_pending_jobs(fake_redis_conn)

    assert jobs.get_job_status(job_id)["stage"] == "salvando"  # última etapa antes de concluir


def test_analyzer_notifies_stages(monkeypatch):
    stages = []
    monkeypatch.setattr(analyzer, "get_reference_images_as_bytes", lambda exam_type: [])
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda api_key, *args: MagicMock())
    monkeypatch.setattr(analyzer, "_generate_with_retry", MagicMock(return_value=MagicMock(text="laudo", usage_metadata=None)))

    analyzer.analyze_exam_from_bytes((b"img-progresso", "image/jpeg"), "joelho.jpg", api_key="k",
                                     user_description="dor", on_progress=stages.append)

    assert stages == ["analisando"]


# ── Laudo: renderização única (tela e PDF) ────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node não instalado")
def test_shared_markdown_renderer_escapes_and_structures():
    script = """
    global.window = {};
    require(process.argv[1]);
    const html = window.renderLaudoMarkdown(
      '## 3. ACHADOS\\n1. **[FORTE]** Rotura <img src=x onerror=alert(1)>\\n2. Edema\\n\\n> descartado: nada');
    process.stdout.write(JSON.stringify({ html, meta: window.laudoPdfMeta({ exam_type: 'Joelho' }, null) }));
    """
    out = subprocess.run(["node", "-e", script, str(ROOT / "static/laudo-ui.js")],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    data = json.loads(out)
    assert "<img" not in data["html"] and "&lt;img" in data["html"]
    assert "<h3>3. ACHADOS</h3>" in data["html"]
    assert "<ol><li><strong>[FORTE]</strong>" in data["html"]
    assert "<blockquote>descartado: nada</blockquote>" in data["html"]
    assert [m["label"] for m in data["meta"]] == ["Região", "Imagens analisadas", "Referências usadas"]


_FEEDBACK_HARNESS = """
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const scripts = [...html.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)].map(m => m[1]);
const pageScript = scripts[scripts.length - 1];

const listeners = {};
const el = (id, extra = {}) => ({
  id, textContent: '', value: '', className: '', disabled: false, dataset: {},
  classList: { toggle() {}, add() {} }, focus() {},
  addEventListener(type, fn) { listeners[id + ':' + type] = fn; }, ...extra,
});
const data = {
  'data-responsible': JSON.stringify({ name: 'Dra. Ana', role: 'Fisioterapeuta' }),
  'data-analysis-id': JSON.stringify('a1'), 'data-user-desc': '""', 'data-consensus': '{}',
  'data-exam-meta': '{}',
};
const nodes = {};
const partial = el('btn-partial', { dataset: { feedback: 'partial' } });
global.document = {
  getElementById: id => nodes[id] || (nodes[id] = el(id, { textContent: data[id] || '' })),
  querySelectorAll: sel => (sel === '[data-feedback]' ? [partial] : []),
};
global.renderLaudoInto = () => {};
let sent = null;
global.fetch = async (url, opts) => {
  sent = { url, opts };
  return { ok: false, status: 302, json: async () => { throw new SyntaxError('The string did not match the expected pattern.'); } };
};

new Function(pageScript)();
listeners['btn-partial:click']();
nodes['feedback-comment'].value = 'imagem ruim';
listeners['submit-feedback:click']().then(() => {
  process.stdout.write(JSON.stringify({
    body: JSON.parse(sent.opts.body), accept: sent.opts.headers.Accept, status: nodes['feedback-status'].textContent,
  }));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node não instalado")
@pytest.mark.parametrize("name", ["result.html", "laudo_detalhe.html"])
def test_feedback_submit_sends_responsible_and_handles_non_json(name):
    # Regressão: "Can't find variable: responsible" ao salvar feedback (variável
    # declarada só dentro de downloadPDF) e erro críptico quando a resposta não é JSON.
    out = subprocess.run(["node", "-e", _FEEDBACK_HARNESS, str(ROOT / "templates" / name)],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    data = json.loads(out)
    assert data["body"]["responsible_name"] == "Dra. Ana"
    assert data["body"]["responsible_role"] == "Fisioterapeuta"
    assert data["accept"] == "application/json"
    assert data["status"] == "Não foi possível salvar o feedback. Tente novamente."


def test_report_pages_use_the_shared_renderer():
    for name in ("result.html", "laudo_detalhe.html", "trial.html"):
        html = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "laudo-ui.js" in html and "renderLaudoInto(" in html
        assert "function renderMarkdown" not in html
