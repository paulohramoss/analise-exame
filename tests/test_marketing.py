"""
Opt-in de e-mail no /trial, descadastro e lembrete de checkout abandonado
(web/marketing.py). Sem rede: banco, Asaas e SMTP são simulados.
"""

from datetime import datetime, timezone

import pytest

from core import asaas, db, mailer
from web import marketing


# ── Opt-in no /trial ──────────────────────────────────────────────────────────

@pytest.fixture()
def leads(monkeypatch):
    salvos = []
    monkeypatch.setattr(db, "salvar_lead", lambda email, origem, consentimento_texto: salvos.append(
        (email, origem, consentimento_texto)) or True)
    return salvos


def test_trial_mostra_optin_desmarcado_com_texto_do_servidor(client):
    html = client.get("/trial").get_data(as_text=True)
    assert marketing.CONSENTIMENTO_MARKETING in html
    assert '<input type="checkbox" id="lead-consent">' in html  # nunca pré-marcado


def test_lead_exige_consentimento(client, leads):
    resp = client.post("/trial/lead", json={"email": "dra@clinica.com.br"})
    assert resp.status_code == 400
    assert leads == []


def test_lead_rejeita_email_invalido(client, leads):
    resp = client.post("/trial/lead", json={"email": "nao-e-email", "consentimento": True})
    assert resp.status_code == 400
    assert leads == []


def test_lead_grava_email_normalizado_com_texto_do_consentimento(client, leads):
    resp = client.post("/trial/lead", json={"email": "  Dra@Clinica.com.BR ", "consentimento": True})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert leads == [("dra@clinica.com.br", "trial", marketing.CONSENTIMENTO_MARKETING)]


def test_lead_honeypot_finge_sucesso_sem_gravar(client, leads):
    resp = client.post("/trial/lead", json={"email": "bot@spam.com", "consentimento": True, "website": "x"})
    assert resp.status_code == 200
    assert leads == []


def test_lead_sem_banco_avisa_o_usuario(client, monkeypatch):
    monkeypatch.setattr(db, "salvar_lead", lambda *a, **k: False)
    resp = client.post("/trial/lead", json={"email": "dra@clinica.com.br", "consentimento": True})
    assert resp.status_code == 503


# ── Descadastro ───────────────────────────────────────────────────────────────

@pytest.fixture()
def descadastros(monkeypatch):
    feitos = []
    monkeypatch.setattr(db, "registrar_descadastro", lambda email, origem="": feitos.append(email) or True)
    return feitos


def test_get_do_link_so_pede_confirmacao(client, descadastros):
    token = marketing.gerar_token_descadastro("paciente@exemplo.com")
    html = client.get(f"/descadastrar?t={token}").get_data(as_text=True)
    assert "Confirmar descadastro" in html
    assert "paciente@exemplo.com" not in html  # e-mail aparece mascarado
    assert descadastros == []


def test_post_confirma_descadastro(client, descadastros):
    token = marketing.gerar_token_descadastro("paciente@exemplo.com")
    resp = client.post("/descadastrar", data={"t": token})
    assert resp.status_code == 200
    assert descadastros == ["paciente@exemplo.com"]


def test_descadastro_um_clique_do_provedor_de_email(client, descadastros):
    """Gmail/Outlook fazem POST no link do List-Unsubscribe, com o token na URL e sem Origin."""
    token = marketing.gerar_token_descadastro("paciente@exemplo.com")
    resp = client.post(f"/descadastrar?t={token}", data={"List-Unsubscribe": "One-Click"})
    assert resp.status_code == 200
    assert descadastros == ["paciente@exemplo.com"]


def test_token_adulterado_e_recusado(client, descadastros):
    resp = client.post("/descadastrar", data={"t": "token-falso"})
    assert resp.status_code == 400
    assert descadastros == []


# ── Rota do cron ──────────────────────────────────────────────────────────────

def test_cron_desligado_sem_segredo(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert client.get("/cron/recuperar-checkout").status_code == 503


def test_cron_recusa_segredo_errado(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "segredo-certo")
    resp = client.get("/cron/recuperar-checkout", headers={"Authorization": "Bearer outro"})
    assert resp.status_code == 401


def test_cron_com_segredo_roda_a_recuperacao(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "segredo-certo")
    monkeypatch.setattr(marketing, "recuperar_checkouts_abandonados", lambda: {"enviados": 2})
    resp = client.get("/cron/recuperar-checkout", headers={"Authorization": "Bearer segredo-certo"})
    assert resp.status_code == 200
    assert resp.get_json() == {"enviados": 2}


# ── Lembrete de checkout abandonado ───────────────────────────────────────────

AGORA = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)


def _cobranca(payment_id, email, nome="Ana Souza", plano="anual"):
    return {
        "asaas_payment_id": payment_id,
        "invoice_url": f"https://asaas.test/i/{payment_id}",
        "plano": plano,
        "created_at": "2026-11-08T10:00:00+00:00",
        "clientes": {"nome": nome, "email": email} if email else None,
    }


@pytest.fixture()
def cenario(monkeypatch):
    """Banco, Asaas e SMTP falsos; devolve o estado para cada teste montar o caso."""
    estado = {
        "cobrancas": [],
        "descadastrados": set(),
        "pagos": set(),
        "status_asaas": {},
        "marcados": [],
        "status_atualizados": [],
        "enviados": [],
        "janela": None,
    }
    monkeypatch.setenv("APP_BASE_URL", "https://app.threehealth.test")
    monkeypatch.setattr(mailer, "is_configured", lambda: True)

    def listar(criado_depois_de, criado_antes_de, limite):
        estado["janela"] = (criado_depois_de, criado_antes_de, limite)
        return estado["cobrancas"]

    def enviar(to, subject, body, headers=None):
        estado["enviados"].append({"to": to, "subject": subject, "body": body, "headers": headers})
        return True

    def consultar(payment_id):
        status = estado["status_asaas"].get(payment_id, "PENDING")
        if isinstance(status, Exception):
            raise status
        return {"status": status}

    monkeypatch.setattr(db, "listar_checkouts_abandonados", listar)
    monkeypatch.setattr(db, "emails_descadastrados", lambda emails: {e for e in emails if e in estado["descadastrados"]})
    monkeypatch.setattr(db, "cliente_tem_plano_pago", lambda email, *a, **k: email in estado["pagos"])
    monkeypatch.setattr(db, "marcar_lembrete_enviado", lambda pid: estado["marcados"].append(pid) or True)

    def reservar(pid):
        if pid in estado["marcados"]:
            return False
        estado["marcados"].append(pid)
        return True

    monkeypatch.setattr(db, "reservar_lembrete", reservar)
    monkeypatch.setattr(db, "liberar_lembrete", lambda pid: estado["marcados"].remove(pid))
    monkeypatch.setattr(db, "atualizar_status_pagamento",
                        lambda pid, status, payload=None: estado["status_atualizados"].append((pid, status)))
    monkeypatch.setattr(asaas, "get_payment", consultar)
    monkeypatch.setattr(mailer, "send_email", enviar)
    return estado


def test_envia_um_lembrete_com_link_da_cobranca_e_descadastro(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", "Ana@Clinica.com.br")]

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert resumo["enviados"] == 1
    assert cenario["marcados"] == ["pay_1"]
    email = cenario["enviados"][0]
    assert email["to"] == "ana@clinica.com.br"
    assert "Olá, Ana!" in email["body"]
    assert "https://asaas.test/i/pay_1" in email["body"]
    assert "https://app.threehealth.test/checkout?plan=anual&src=email_recuperacao" in email["body"]
    link_sair = email["headers"]["List-Unsubscribe"].strip("<>")
    assert link_sair in email["body"]
    token = link_sair.split("t=", 1)[1]
    assert marketing.email_do_token_descadastro(token) == "ana@clinica.com.br"
    assert email["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_janela_padrao_de_3_a_72_horas(cenario):
    marketing.recuperar_checkouts_abandonados(agora=AGORA)
    depois_de, antes_de, limite = cenario["janela"]
    assert depois_de == "2026-11-06T13:00:00+00:00"
    assert antes_de == "2026-11-09T10:00:00+00:00"
    assert limite == 25


def test_nao_envia_para_quem_se_descadastrou(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    cenario["descadastrados"] = {"ana@clinica.com.br"}

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []
    assert resumo["pulados"] == 1
    assert cenario["marcados"] == ["pay_1"]  # sai da fila de vez


def test_nao_cobra_quem_ja_pagou_e_o_webhook_atrasou(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    cenario["status_asaas"] = {"pay_1": "RECEIVED"}

    marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []
    assert cenario["status_atualizados"] == [("pay_1", "RECEIVED")]


def test_nao_envia_para_quem_ja_tem_outro_plano_pago(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    cenario["pagos"] = {"ana@clinica.com.br"}

    marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []


def test_asaas_fora_do_ar_nao_envia_e_tenta_de_novo_amanha(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    cenario["status_asaas"] = {"pay_1": RuntimeError("timeout")}

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []
    assert cenario["marcados"] == []
    assert resumo["falhas"] == 1


def test_um_lembrete_por_pessoa_mesmo_com_duas_cobrancas(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br"), _cobranca("pay_2", "ana@clinica.com.br", plano="mensal")]

    marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert len(cenario["enviados"]) == 1
    assert cenario["marcados"] == ["pay_1", "pay_2"]


def test_conta_excluida_nao_recebe_lembrete(cenario):
    cenario["cobrancas"] = [_cobranca("pay_1", None)]

    marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []


def test_lista_de_descadastro_indisponivel_bloqueia_tudo(cenario, monkeypatch):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    monkeypatch.setattr(db, "emails_descadastrados", lambda emails: None)

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []
    assert resumo["falhas"] == 1


def test_falha_no_smtp_mantem_a_cobranca_na_fila(cenario, monkeypatch):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: False)

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["marcados"] == []
    assert resumo["falhas"] == 1


def test_sem_smtp_fica_desligado(cenario, monkeypatch):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    monkeypatch.setattr(mailer, "is_configured", lambda: False)

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert "desligado" in resumo
    assert cenario["enviados"] == []


# ── Consulta no banco ─────────────────────────────────────────────────────────

def test_consulta_de_abandonados_so_pega_cobranca_em_aberto_sem_lembrete(monkeypatch):
    from tests.test_db_queries import _use_fake_client

    fake = _use_fake_client(monkeypatch)
    db.listar_checkouts_abandonados("2026-11-06T13:00:00+00:00", "2026-11-09T10:00:00+00:00", 25)

    assert ("table", ("pagamentos",), {}) in fake.calls
    assert ("is_", ("lembrete_enviado_em", "null"), {}) in fake.calls
    assert ("or_", ("status.is.null,status.in.(PENDING,OVERDUE)",), {}) in fake.calls
    assert ("limit", (25,), {}) in fake.calls


def test_descadastro_apaga_o_lead_e_entra_na_lista_de_supressao(monkeypatch):
    from tests.test_db_queries import _use_fake_client

    fake = _use_fake_client(monkeypatch)
    assert db.registrar_descadastro("ana@clinica.com.br", origem="link")

    tabelas = [args[0] for name, args, _ in fake.calls if name == "table"]
    assert tabelas == ["email_descadastros", "leads"]
    assert ("delete", (), {}) in fake.calls


def test_cobranca_ja_reservada_por_outra_execucao_nao_recebe_segundo_lembrete(cenario, monkeypatch):
    """Duas execuções do cron ao mesmo tempo: só quem reservar a linha envia."""
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    monkeypatch.setattr(db, "reservar_lembrete", lambda pid: False)

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []
    assert resumo["pulados"] == 1


def test_banco_fora_na_consulta_de_plano_pago_nao_envia(cenario, monkeypatch):
    cenario["cobrancas"] = [_cobranca("pay_1", "ana@clinica.com.br")]
    monkeypatch.setattr(db, "cliente_tem_plano_pago", lambda email, *a, **k: None)

    resumo = marketing.recuperar_checkouts_abandonados(agora=AGORA)

    assert cenario["enviados"] == []
    assert cenario["marcados"] == []
    assert resumo["falhas"] == 1


def test_reserva_so_pega_cobranca_ainda_sem_lembrete(monkeypatch):
    from tests.test_db_queries import _use_fake_client

    fake = _use_fake_client(monkeypatch)  # sem linhas: outro processo já reservou
    assert db.reservar_lembrete("pay_1") is False
    assert ("is_", ("lembrete_enviado_em", "null"), {}) in fake.calls


def test_plano_pago_distingue_erro_de_nao_pagou(monkeypatch):
    class _Quebrado:
        def table(self, name):
            raise RuntimeError("supabase fora")

    monkeypatch.setattr(db, "_get_client", lambda: _Quebrado())
    assert db.cliente_tem_plano_pago("ana@clinica.com.br") is None

    from tests.test_db_queries import _use_fake_client

    fake = _use_fake_client(monkeypatch)
    assert db.cliente_tem_plano_pago("ana@clinica.com.br") is False
    in_call = next(args for name, args, _ in fake.calls if name == "in_" and args[0] == "external_reference")
    assert in_call[1] == ["ana@clinica.com.br", "ana@clinica.com.br|mensal", "ana@clinica.com.br|semestral",
                          "ana@clinica.com.br|anual"]
