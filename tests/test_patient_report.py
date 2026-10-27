"""
Relatório do laudo para o paciente: texto da IA (só o laudo vai no prompt),
HTML seguro, logo reprocessado, acesso só do dono do laudo e a marca da
clínica em "Meus dados". Nenhuma chamada real à API da Anthropic.
"""

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from core import db, patient_report
from web import security

LAUDO = {
    "id": "laudo-1", "cliente_id": "cliente-a", "paciente_id": "pac-a", "tipo_exame": "joelho",
    "created_at": "2026-10-20T10:00:00+00:00", "analise_completa": "## ACHADOS\nRuptura do LCA.",
    "responsavel_nome": "Dra. Ana",
}


def _png(size=(800, 300)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(out, format="PNG")
    return out.getvalue()


# ── Texto e HTML ──────────────────────────────────────────────────────────────

def test_html_escapa_o_texto_do_modelo():
    html = patient_report.to_html("## O que o exame mostrou\n<script>x</script> **lesão**\n- item 1\n- item 2")
    assert "<h2>O que o exame mostrou</h2>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>lesão</strong>" in html
    assert "<ul>\n<li>item 1</li>\n<li>item 2</li>\n</ul>" in html


def _fake_anthropic(monkeypatch, response):
    client = MagicMock()
    client.beta.messages.create.return_value = response
    monkeypatch.setattr(patient_report.anthropic, "Anthropic", lambda **kwargs: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "teste")
    return client


def _response(text="## O que o exame mostrou\nTexto.", stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason, model=patient_report.MODEL,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=600),
    )


def test_gerar_texto_envia_so_o_laudo_com_fallback(monkeypatch):
    client = _fake_anthropic(monkeypatch, _response())

    texto, usage = patient_report.gerar_texto(LAUDO["analise_completa"])

    assert texto.startswith("## O que o exame mostrou")
    assert usage == {"model": patient_report.MODEL, "input_tokens": 1200, "output_tokens": 600}
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == patient_report.MODEL
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["output_config"] == {"effort": "low"}
    assert "temperature" not in kwargs
    assert kwargs["messages"] == [{"role": "user", "content": f"Laudo do exame:\n\n{LAUDO['analise_completa']}"}]


def test_recusa_do_modelo_nao_vira_relatorio(monkeypatch):
    _fake_anthropic(monkeypatch, _response(stop_reason="refusal"))
    assert patient_report.gerar_texto("laudo") is None


def test_sem_chave_da_anthropic_nao_chama_a_api(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert patient_report.gerar_texto("laudo") is None


# ── Logo ──────────────────────────────────────────────────────────────────────

def test_logo_e_reduzido_e_vira_png():
    data_url = patient_report.processar_logo(_png())
    assert data_url.startswith("data:image/png;base64,")
    import base64
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as img:
        assert max(img.size) <= 400


@pytest.mark.parametrize("raw", [b"", b"<svg onload=alert(1)>", b"x" * (patient_report.LOGO_MAX_BYTES + 1)])
def test_logo_invalido_e_recusado(raw):
    assert patient_report.processar_logo(raw) is None


# ── Rotas ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def profissional(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(
        db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False:
            dict(LAUDO) if analise_id == "laudo-1" and cliente_id == "cliente-a" else None,
    )
    monkeypatch.setattr(db, "buscar_paciente", lambda pid, cid: {"id": pid, "nome": "Maria da Silva"})
    monkeypatch.setattr(db, "buscar_marca_clinica", lambda cid: {"clinica_nome": "Clínica Movimento"})
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))
    return client


def test_sem_relatorio_mostra_botao_de_gerar(profissional, monkeypatch):
    monkeypatch.setattr(db, "buscar_relatorio_paciente", lambda aid, cid: None)
    html = profissional.get("/laudos/laudo-1/paciente").get_data(as_text=True)
    assert "Gerar relatório" in html


def test_relatorio_mostra_marca_e_nome_do_paciente(profissional, monkeypatch):
    monkeypatch.setattr(db, "buscar_relatorio_paciente", lambda aid, cid: "## O que o exame mostrou\nUma lesão.")
    html = profissional.get("/laudos/laudo-1/paciente").get_data(as_text=True)
    assert "Clínica Movimento" in html and "Maria da Silva" in html
    assert "<h2>O que o exame mostrou</h2>" in html


def test_gerar_salva_o_texto_e_nao_envia_nome_a_ia(profissional, monkeypatch):
    gerar = MagicMock(return_value=("## O que o exame mostrou\nTexto.", {"model": "claude-opus-5-5", "input_tokens": 10, "output_tokens": 10}))
    monkeypatch.setattr(patient_report, "gerar_texto", gerar)
    salvar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "salvar_relatorio_paciente", salvar)

    resp = profissional.post("/laudos/laudo-1/paciente/gerar")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/laudos/laudo-1/paciente")
    salvar.assert_called_once_with("laudo-1", "cliente-a", "## O que o exame mostrou\nTexto.")
    assert "Maria" not in str(gerar.call_args)


def test_laudo_de_outro_profissional_nao_gera(profissional, monkeypatch):
    gerar = MagicMock()
    monkeypatch.setattr(patient_report, "gerar_texto", gerar)
    resp = profissional.post("/laudos/laudo-de-outro/paciente/gerar")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/laudos")
    gerar.assert_not_called()


def test_salvar_marca_da_clinica_com_logo(profissional, monkeypatch):
    salvar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "salvar_marca_clinica", salvar)

    resp = profissional.post(
        "/conta/clinica",
        data={"clinica_nome": "  Clínica   Movimento ", "clinica_contato": "(11) 9999-9999",
              "clinica_logo": (io.BytesIO(_png()), "logo.png")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 302
    cliente_id, campos = salvar.call_args.args
    assert cliente_id == "cliente-a" and campos["clinica_nome"] == "Clínica Movimento"
    assert campos["clinica_logo"].startswith("data:image/png;base64,")


def test_migration_pendente_nao_quebra_o_relatorio(monkeypatch):
    """Sem as colunas novas, a consulta falha e o relatório só fica vazio."""
    broken = MagicMock()
    broken.table.side_effect = RuntimeError("column analises.relatorio_paciente does not exist")
    monkeypatch.setattr(db, "_get_client", lambda: broken)
    assert db.buscar_relatorio_paciente("laudo-1", "cliente-a") is None
    assert db.buscar_marca_clinica("cliente-a") == {}
