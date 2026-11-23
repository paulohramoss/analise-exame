"""Edição do laudo: a versão da IA fica guardada, só o dono edita, o resumo não leva texto clínico."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import db, laudo_edicao
from web import security

IA = """## 0. SINAIS DE ALERTA E ENCAMINHAMENTO
Nenhum sinal de alerta.

## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
- [FORTE] Ruptura completa do LCA
- [MODERADO] Edema ósseo no côndilo femoral lateral
- [BAIXO] Lesão do menisco medial

## 4. IMPRESSÃO DE IMAGEM — HIPÓTESES
Ruptura do LCA com contusão óssea."""

PROF = """## 0. SINAIS DE ALERTA E ENCAMINHAMENTO
Nenhum sinal de alerta.

## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
- [FORTE] Ruptura completa do LCA
- [MODERADO] Edema ósseo no côndilo femoral lateral
- [MODERADO] Derrame articular moderado

## 4. IMPRESSÃO DE IMAGEM — HIPÓTESES
Ruptura do LCA com contusão óssea."""


def test_resumo_conta_achados_por_secao_sem_copiar_texto():
    r = laudo_edicao.resumir(IA, PROF)
    assert r["linhas_ia"] == 5 and r["linhas_removidas"] == 1 and r["linhas_adicionadas"] == 1
    assert r["percentual_mantido"] == 80
    assert r["secoes"] == [{
        "secao": "3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA", "removida": False, "nova": False,
        "achados_removidos": 1, "achados_adicionados": 1,
    }]
    assert "menisco" not in str(r) and "Derrame" not in str(r)


def test_normalizar_ignora_crlf_e_espacos_finais():
    assert laudo_edicao.normalizar("a  \r\nb\r\n\r\n") == "a\nb"
    assert laudo_edicao.resumir(IA, IA.replace("\n", "\r\n"))["secoes"] == []


def test_agregar_conta_nao_editados_como_confirmados():
    resumos = [laudo_edicao.resumir(IA, PROF)]
    m = laudo_edicao.agregar(resumos, total_laudos=4)
    assert m["editados"] == 1 and m["percentual_editados"] == 25
    assert m["percentual_confirmado"] == round((300 + 80) / 4)
    assert m["secoes_mais_corrigidas"][0]["secao"].startswith("ACHADOS")
    assert laudo_edicao.agregar([], 0)["percentual_confirmado"] == 0


# ── Rotas ─────────────────────────────────────────────────────────────────────

LAUDO = {
    "id": "laudo-1", "cliente_id": "cliente-a", "paciente_id": None, "tipo_exame": "joelho",
    "created_at": "2026-10-20T10:00:00+00:00", "analise_completa": IA, "num_imagens": 1,
}


@pytest.fixture()
def profissional(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(
        db, "buscar_analise",
        lambda analise_id, cliente_id=None, include_all=False:
            dict(LAUDO) if analise_id == "laudo-1" and cliente_id == "cliente-a" else None,
    )
    monkeypatch.setattr(db, "buscar_dados_estruturados", lambda *a, **k: None)
    monkeypatch.setattr(db, "listar_pacientes", lambda cid: [])
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))
    return client


def test_editar_guarda_versao_da_ia_e_resumo(profissional, monkeypatch):
    monkeypatch.setattr(db, "buscar_edicao_laudo", lambda *a, **k: None)
    salvar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "salvar_edicao_laudo", salvar)

    resp = profissional.post("/laudos/laudo-1/editar", data={"analise": PROF.replace("\n", "\r\n")})

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/laudos/laudo-1")
    analise_id, cliente_id, texto, texto_ia, resumo = salvar.call_args.args
    assert (analise_id, cliente_id, texto, texto_ia) == ("laudo-1", "cliente-a", PROF, IA)
    assert resumo["percentual_mantido"] == 80


def test_segunda_edicao_compara_com_a_ia_original(profissional, monkeypatch):
    laudo = dict(LAUDO, analise_completa=PROF)
    monkeypatch.setattr(db, "buscar_analise", lambda *a, **k: laudo)
    monkeypatch.setattr(db, "buscar_edicao_laudo", lambda *a, **k: {"analise_ia": IA, "editado_em": "x"})
    salvar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "salvar_edicao_laudo", salvar)

    profissional.post("/laudos/laudo-1/editar", data={"analise": PROF + "\nObs. final."})
    assert salvar.call_args.args[3] == IA

    profissional.post("/laudos/laudo-1/editar", data={"acao": "restaurar_ia"})
    assert salvar.call_args.args[2] == IA and salvar.call_args.args[4]["percentual_mantido"] == 100


@pytest.mark.parametrize("texto", ["", "   ", IA])
def test_edicao_vazia_ou_sem_mudanca_nao_salva(profissional, monkeypatch, texto):
    monkeypatch.setattr(db, "buscar_edicao_laudo", lambda *a, **k: None)
    salvar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "salvar_edicao_laudo", salvar)
    profissional.post("/laudos/laudo-1/editar", data={"analise": texto})
    salvar.assert_not_called()


def test_laudo_de_outro_profissional_nao_edita(profissional, monkeypatch):
    salvar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "salvar_edicao_laudo", salvar)
    resp = profissional.post("/laudos/laudo-de-outro/editar", data={"analise": PROF})
    assert resp.headers["Location"].endswith("/laudos")
    salvar.assert_not_called()


def test_detalhe_mostra_editor_e_versao_da_ia(profissional, monkeypatch):
    monkeypatch.setattr(db, "buscar_analise", lambda *a, **k: dict(LAUDO, analise_completa=PROF))
    monkeypatch.setattr(db, "buscar_edicao_laudo", lambda *a, **k: {
        "analise_ia": IA, "editado_em": "2026-10-21T10:00:00+00:00", "edicao_resumo": {"percentual_mantido": 83},
    })
    html = profissional.get("/laudos/laudo-1").get_data(as_text=True)
    assert "Editar laudo" in html and "Restaurar versão da IA" in html
    assert "Ver a versão original da IA" in html and "83% do texto da IA mantido" in html


def test_salvar_edicao_nao_sobrescreve_versao_da_ia(monkeypatch):
    calls = []

    class _Q:
        def __init__(self, data):
            self.data = data

        def __getattr__(self, name):
            def rec(*a, **k):
                calls.append((name, a))
                return self
            return rec

        def execute(self):
            return SimpleNamespace(data=self.data)

    results = iter([[{"analise_ia": "ORIGINAL"}], [{"id": "laudo-1"}]])
    monkeypatch.setattr(db, "_get_client", lambda: SimpleNamespace(table=lambda _n: _Q(next(results))))
    assert db.salvar_edicao_laudo("laudo-1", "cliente-a", "novo", "ia-de-agora", {"x": 1})
    update = next(a[0] for n, a in calls if n == "update")
    assert "analise_ia" not in update and update["analise_completa"] == "novo"
    assert ("eq", ("cliente_id", "cliente-a")) in calls


def test_migration_pendente_nao_quebra_o_laudo(monkeypatch):
    broken = MagicMock()
    broken.table.side_effect = RuntimeError("column analises.analise_ia does not exist")
    monkeypatch.setattr(db, "_get_client", lambda: broken)
    assert db.buscar_edicao_laudo("laudo-1", "cliente-a") is None
    assert db.listar_resumos_edicao("cliente-a") == []
    assert db.salvar_edicao_laudo("laudo-1", "cliente-a", "a", "b", {}) is False
