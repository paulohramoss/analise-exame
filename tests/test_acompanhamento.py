"""Metas por paciente, lembretes de reavaliação, tela "Hoje" e e-mail diário."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from core import acompanhamento, db, dynamometry, lab_tests, mailer
from web import prontuario, security

HOJE = date(2026, 6, 1)


def _lab(id_, criado, **valores):
    resultados = [{"exame": nome, "valor": v, "valor_texto": "", "unidade": "ng/mL", "referencia_min": mn,
                   "referencia_max": mx, "referencia_texto": "", "anteriores": []}
                  for nome, (v, mn, mx) in valores.items()]
    dados = lab_tests.compute({"resultados": resultados, "laboratorio": "", "data_coleta": None, "paciente": {}})
    return {"id": id_, "created_at": criado, "tipo_exame": "laboratorial", "dados": dados}


def _dyn(id_, criado, esq):
    dados = dynamometry.compute({
        "lado_acometido": "esquerdo", "massa_corporal_kg": None, "plataforma": [],
        "medidas": [{"grupo_muscular": "Quadríceps", "acao": "concentrica", "velocidade_graus_s": 60,
                     "angulo_graus": None, "unidade": "Nm", "pico_direito": 200, "pico_esquerdo": esq}],
    })
    return {"id": id_, "created_at": criado, "tipo_exame": "dinamometria", "dados": dados}


def test_meta_lsi_casa_por_palavras_e_mostra_tendencia():
    analises = [_dyn("d1", "2026-03-01T10:00:00", 160), _dyn("d2", "2026-05-20T10:00:00", 172)]
    meta = {"id": "m1", "item": "LSI quadriceps", "operador": ">=", "valor": 90, "unidade": "%", "prazo": None}
    r = acompanhamento.avaliar(meta, acompanhamento.medicoes(analises), HOJE)
    assert r["status"] == "nao_atingida" and r["ultimo"]["valor"] == 86.0
    assert r["tendencia"] == "aproximando" and r["progresso_pct"] == 60
    assert r["descricao"] == "LSI quadriceps ≥ 90 %"


def test_meta_atingida_no_exame_novo_vira_alerta():
    analises = [_lab("l1", "2026-03-01T10:00:00", Ferritina=(18, 30, 300)),
                _lab("l2", "2026-05-25T10:00:00", Ferritina=(62, 30, 300))]
    metas = [{"id": "m1", "item": "Ferritina", "operador": ">", "valor": 50, "unidade": "ng/mL", "prazo": None}]
    avaliadas = acompanhamento.avaliar_todas(metas, analises, HOJE)
    assert avaliadas[0]["status"] == "atingida" and avaliadas[0]["tendencia"] == "atingiu"
    assert acompanhamento.alertas_das_metas(avaliadas, HOJE) == avaliadas


def test_meta_que_se_afasta_e_vencida():
    analises = [_lab("l1", "2026-03-01T10:00:00", LDL=(121, None, 130)),
                _lab("l2", "2026-05-25T10:00:00", LDL=(153, None, 130))]
    metas = [{"id": "m1", "item": "LDL", "operador": "<", "valor": 100, "prazo": "2026-05-01"}]
    r = acompanhamento.avaliar_todas(metas, analises, HOJE)[0]
    assert r["status"] == "vencida" and r["tendencia"] == "afastando"


def test_meta_livre_e_sem_dado():
    valores = []
    livre = {"id": "m", "item": "Retorno ao esporte", "operador": None, "valor": None, "prazo": "2026-06-05"}
    assert acompanhamento.avaliar(livre, valores, HOJE)["status"] == "em_andamento"
    assert acompanhamento.avaliar({**livre, "concluida_em": "x"}, valores, HOJE)["status"] == "atingida"
    assert acompanhamento.alertas_das_metas([acompanhamento.avaliar(livre, valores, HOJE)], HOJE)  # prazo em 4 dias
    sem = {"id": "m", "item": "CK", "operador": "<", "valor": 200, "prazo": None}
    assert acompanhamento.avaliar(sem, valores, HOJE)["status"] == "sem_dado"


def test_data_do_lembrete():
    assert acompanhamento.data_do_lembrete({"em_quantidade": "2", "em_unidade": "semanas"}, HOJE) == date(2026, 6, 15)
    assert acompanhamento.data_do_lembrete({"data": "2026-07-01", "em_quantidade": "2"}, HOJE) == date(2026, 7, 1)
    assert acompanhamento.data_do_lembrete({"em_quantidade": "0", "em_unidade": "dias"}, HOJE) is None
    assert acompanhamento.data_do_lembrete({"em_quantidade": "x"}, HOJE) is None


def test_separar_lembretes():
    g = acompanhamento.separar_lembretes([
        {"id": "1", "data": "2026-05-30"}, {"id": "2", "data": "2026-06-01"},
        {"id": "3", "data": "2026-06-10"}, {"id": "4", "data": "2026-05-01", "concluido_em": "x"},
    ], HOJE)
    assert [x["id"] for x in g["atrasados"]] == ["1"] and g["atrasados"][0]["dias"] == -2
    assert [x["id"] for x in g["hoje"]] == ["2"] and [x["id"] for x in g["proximos"]] == ["3"]


@pytest.mark.parametrize("form, erro", [
    ({"item": "LSI", "operador": ">=", "valor": "90"}, False),
    ({"item": "Ferritina", "operador": ">", "valor": "50,5"}, False),
    ({"item": "Retorno ao esporte", "prazo": "2026-09-01"}, False),
    ({"item": "Retorno ao esporte"}, True),            # meta livre sem prazo
    ({"item": "LSI", "operador": ">="}, True),          # comparação sem valor
    ({"item": "LSI", "valor": "90"}, True),             # valor sem comparação
    ({"item": "LSI", "operador": "!=", "valor": "1"}, True),
    ({"item": "x", "operador": ">", "valor": "1"}, True),
])
def test_meta_from_form(form, erro):
    _campos, errors = prontuario.meta_from_form(form)
    assert bool(errors) is erro


# ── Rotas ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def profissional(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_paciente",
                        lambda pid, cid: {"id": pid, "nome": "Maria"} if (pid, cid) == ("pac-1", "cliente-a") else None)
    monkeypatch.setattr(db, "registrar_acesso_prontuario", MagicMock())
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))
    return client


def test_criar_meta_e_lembrete(profissional, monkeypatch):
    salvar_meta = MagicMock(return_value="m1")
    salvar_lembrete = MagicMock(return_value="l1")
    monkeypatch.setattr(db, "salvar_meta", salvar_meta)
    monkeypatch.setattr(db, "salvar_lembrete", salvar_lembrete)

    resp = profissional.post("/pacientes/pac-1/metas", data={"item": "LSI quadríceps", "operador": ">=", "valor": "90"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#metas")
    assert salvar_meta.call_args.args[:2] == ("pac-1", "cliente-a")
    assert salvar_meta.call_args.args[2]["valor"] == 90.0

    profissional.post("/pacientes/pac-1/lembretes", data={"texto": "Repetir CK", "em_quantidade": "2", "em_unidade": "semanas"})
    cliente_id, paciente_id, texto, _data = salvar_lembrete.call_args.args
    assert (cliente_id, paciente_id, texto) == ("cliente-a", "pac-1", "Repetir CK")


def test_meta_de_paciente_alheio_404(profissional, monkeypatch):
    salvar = MagicMock()
    monkeypatch.setattr(db, "salvar_meta", salvar)
    assert profissional.post("/pacientes/pac-x/metas", data={"item": "LSI", "operador": ">=", "valor": "9"}).status_code == 404
    salvar.assert_not_called()


def test_concluir_lembrete_nao_redireciona_para_fora(profissional, monkeypatch):
    concluir = MagicMock(return_value=True)
    monkeypatch.setattr(db, "concluir_lembrete", concluir)
    resp = profissional.post("/lembretes/l1/concluir", data={"voltar": "//evil.example/x"})
    assert resp.headers["Location"].endswith("/hoje")
    concluir.assert_called_once_with("l1", "cliente-a", concluido=True)


def test_tela_hoje(profissional, monkeypatch):
    monkeypatch.setattr(db, "listar_pacientes", lambda cid: [{"id": "pac-1", "nome": "Maria"}])
    monkeypatch.setattr(db, "listar_lembretes", lambda cid, pid=None: [
        {"id": "l1", "paciente_id": "pac-1", "texto": "Repetir CK", "data": date.today().isoformat()},
    ])
    monkeypatch.setattr(db, "listar_metas", lambda cid, pid=None: [
        {"id": "m1", "paciente_id": "pac-1", "item": "Ferritina", "operador": ">", "valor": 50, "prazo": None},
    ])
    hoje = date.today().isoformat()
    monkeypatch.setattr(db, "listar_dados_estruturados_pacientes", lambda cid, pid=None: [
        {**_lab("l1", "2026-01-01T10:00:00", Ferritina=(18, 30, 300)), "paciente_id": "pac-1"},
        {**_lab("l2", f"{hoje}T10:00:00", Ferritina=(62, 30, 300)), "paciente_id": "pac-1"},
    ])
    monkeypatch.setattr(db, "listar_analises", lambda **k: [{"id": "a1", "created_at": f"{hoje}T09:00:00", "tipo_exame": "joelho"}])
    monkeypatch.setattr(db, "listar_validacoes_clinicas", lambda ids: [])

    html = profissional.get("/hoje").get_data(as_text=True)
    assert "Repetir CK" in html and "Maria" in html
    assert "atingiu a meta no último exame" in html
    assert "/laudos/a1" in html


def test_cron_lembretes_sem_nome_de_paciente(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3")
    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    send = MagicMock(return_value=True)
    monkeypatch.setattr(mailer, "send_email", send)
    marcar = MagicMock(return_value=True)
    monkeypatch.setattr(db, "marcar_lembretes_email", marcar)
    monkeypatch.setattr(db, "listar_lembretes_para_email", lambda dia: [
        {"id": "l1", "cliente_id": "c1", "email": "ana@example.com", "nome": "Ana Souza"},
        {"id": "l2", "cliente_id": "c1", "email": "ana@example.com", "nome": "Ana Souza"},
    ])

    assert client.get("/cron/lembretes").status_code == 401
    resp = client.get("/cron/lembretes", headers={"Authorization": "Bearer s3"})
    assert resp.get_json() == {"enviados": 1, "profissionais": 1}
    to, assunto, corpo = send.call_args.args
    assert to == "ana@example.com" and "2 reavaliações" in assunto and "Ana" in corpo
    marcar.assert_called_once_with(["l1", "l2"])


def test_migration_pendente_nao_quebra_prontuario(monkeypatch):
    broken = MagicMock()
    broken.table.side_effect = RuntimeError("relation metas_paciente does not exist")
    monkeypatch.setattr(db, "_get_client", lambda: broken)
    assert db.listar_metas("c1", "p1") == [] and db.listar_lembretes("c1") == []
    assert db.salvar_meta("p1", "c1", {"item": "x"}) is None


def test_prontuario_mostra_metas_e_lembretes(profissional, monkeypatch):
    monkeypatch.setattr(db, "listar_analises_paciente", lambda pid, cid: [])
    monkeypatch.setattr(db, "listar_tratamentos", lambda pid, cid: [])
    monkeypatch.setattr(db, "listar_evolucoes", lambda pid, cid: [])
    monkeypatch.setattr(db, "listar_metas", lambda cid, pid=None: [
        {"id": "m1", "paciente_id": "pac-1", "item": "LSI quadríceps", "operador": ">=", "valor": 90, "unidade": "%", "prazo": None},
        {"id": "m2", "paciente_id": "pac-1", "item": "Retorno ao esporte", "operador": None, "valor": None, "prazo": "2099-01-01"},
    ])
    monkeypatch.setattr(db, "listar_dados_estruturados_pacientes", lambda cid, pid=None: [
        _dyn("d1", "2026-03-01T10:00:00", 160), _dyn("d2", "2026-05-20T10:00:00", 172),
    ])
    monkeypatch.setattr(db, "listar_lembretes", lambda cid, pid=None: [
        {"id": "l1", "paciente_id": "pac-1", "texto": "Repetir CK", "data": "2000-01-01"},
    ])
    html = profissional.get("/pacientes/pac-1").get_data(as_text=True)
    assert "LSI quadríceps ≥ 90 %" in html and "86 %" in html and "aproximando-se" in html
    assert "Marcar como cumprida" in html and "Repetir CK" in html and "atrasado" in html
    assert 'option value="LSI Quadríceps · concentrica · 60°/s"' in html
