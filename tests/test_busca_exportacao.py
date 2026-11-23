"""Busca única e exportação do prontuário do paciente em CSV."""

from unittest.mock import MagicMock

import pytest

from core import db
from web import prontuario, security


@pytest.fixture()
def profissional(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_paciente",
                        lambda pid, cid: {"id": pid, "nome": "Maria", "cpf": "529.982.247-25", "sexo": "Feminino"}
                        if (pid, cid) == ("pac-1", "cliente-a") else None)
    monkeypatch.setattr(db, "registrar_acesso_prontuario", MagicMock())
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))
    return client


def test_trechos_em_volta_do_termo():
    texto = "## 3. ACHADOS\n- **[FORTE]** Rotura do menisco medial, corno posterior.\n" + "x " * 200 + "menisco lateral íntegro"
    t = prontuario.trechos(texto, "MENISCO")
    assert len(t) == 2 and "Rotura do menisco medial" in t[0] and "**" not in t[0]


def test_busca_acha_paciente_e_achado(profissional, monkeypatch):
    monkeypatch.setattr(db, "listar_pacientes", lambda cid: [
        {"id": "pac-1", "nome": "Maria da Silva", "cpf": "529.982.247-25", "cpf_mascarado": "***.982.247-**"},
        {"id": "pac-2", "nome": "José", "cpf": "111.444.777-35", "cpf_mascarado": "***.444.777-**"},
    ])
    buscar = MagicMock(return_value=[{"id": "l1", "created_at": "2026-05-01T10:00:00", "tipo_exame": "joelho",
                                      "paciente_id": "pac-1", "analise_completa": "Rotura do menisco medial."}])
    monkeypatch.setattr(db, "buscar_laudos_por_texto", buscar)

    html = profissional.get("/busca?q=menisco").get_data(as_text=True)
    assert "Rotura do menisco medial." in html and "/laudos/l1" in html
    buscar.assert_called_once_with("cliente-a", "menisco")

    html = profissional.get("/busca?q=982247").get_data(as_text=True)
    assert "Maria da Silva" in html and "José" not in html


def test_exportar_csv(profissional, monkeypatch):
    monkeypatch.setattr(db, "listar_analises_paciente", lambda pid, cid: [{"id": "l1", "created_at": "2026-05-01", "tipo_exame": "joelho"}])
    monkeypatch.setattr(db, "listar_dados_estruturados_pacientes", lambda cid, pid=None: [])
    monkeypatch.setattr(db, "listar_tratamentos", lambda pid, cid: [{"data_inicio": "2026-05-02", "tipo": "Fisioterapia", "descricao": "=HYPERLINK(1)"}])
    monkeypatch.setattr(db, "listar_evolucoes", lambda pid, cid: [{"data": "2026-05-10", "dor_eva": 4, "funcao": 70, "nota": ""}])
    monkeypatch.setattr(db, "listar_metas", lambda cid, pid=None: [])

    resp = profissional.get("/pacientes/pac-1/exportar.csv")
    corpo = resp.get_data(as_text=True)
    assert resp.mimetype == "text/csv" and "attachment" in resp.headers["Content-Disposition"]
    assert corpo.startswith("\ufeffcategoria;data;item")
    assert "Maria" in corpo and "Fisioterapia" in corpo and "dor (EVA);4" in corpo
    assert "'=HYPERLINK(1)" in corpo  # fórmula neutralizada
    db.registrar_acesso_prontuario.assert_called_with("cliente-a", "pac-1", "exportar")
    assert profissional.get("/pacientes/pac-x/exportar.csv").status_code == 404


def test_csv_neutraliza_formula():
    # Detalhe do tratamento começando com "=" não pode virar fórmula no Excel.
    from web.routes import pacientes

    assert pacientes._numero("-3.5") and not pacientes._numero("=1+1")
