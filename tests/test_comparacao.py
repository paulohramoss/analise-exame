"""Retorno do paciente: exame novo × anterior, item a item, sem IA."""

from unittest.mock import MagicMock

import pytest

from core import comparacao, db, dynamometry, lab_tests
from web import security


def _lab(**valores):
    resultados = []
    for exame, (valor, mn, mx) in valores.items():
        resultados.append({"exame": exame.replace("_", " "), "valor": valor, "valor_texto": "", "unidade": "mg/dL",
                           "referencia_min": mn, "referencia_max": mx, "referencia_texto": "", "anteriores": []})
    return lab_tests.compute({"resultados": resultados, "laboratorio": "", "data_coleta": None, "paciente": {}})


def _por_item(linhas):
    return {linha["item"]: linha for linha in linhas}


def test_laboratorial_direcao_pela_faixa():
    antes = _lab(LDL=(121, None, 130), Ferritina=(18, 30, 300), CK=(900, 30, 200), Glicose=(85, 70, 99), TSH=(2, 0.4, 4))
    depois = _lab(LDL=(153, None, 130), Ferritina=(55, 30, 300), CK=(400, 30, 200), Glicose=(95, 70, 99), Ureia=(30, 15, 45))
    linhas = _por_item(comparacao.comparar_laboratorial(antes, depois))

    assert linhas["LDL"]["direcao"] == "piorou" and linhas["LDL"]["antes_fmt"] == "121 mg/dL"
    assert linhas["Ferritina"]["direcao"] == "melhorou"
    assert linhas["CK"]["direcao"] == "melhorou"  # ainda alta, mas se aproximou da faixa
    assert linhas["Glicose"]["direcao"] == "neutra"  # mudou dentro da faixa
    assert linhas["Ureia"]["direcao"] == "nova" and linhas["TSH"]["direcao"] == "ausente"
    assert linhas["LDL"]["delta_fmt"] == "+32 mg/dL"


def test_laboratorial_casa_nome_sem_acento_e_avisa_unidade_diferente():
    antes = _lab(Vitamina_D=(18, 30, 100))
    depois = _lab(**{"vitamina-d": (35, 30, 100)})
    depois["resultados"][0]["exame"] = "Vitamina D"
    antes["resultados"][0]["exame"] = "VITAMINA D"
    assert comparacao.comparar_laboratorial(antes, depois)[0]["direcao"] == "melhorou"
    depois["resultados"][0]["unidade"] = "nmol/L"
    assert comparacao.comparar_laboratorial(antes, depois)[0]["direcao"] == "unidade_diferente"


def test_dinamometria_lsi_maior_e_melhor():
    def dyn(dir_, esq):
        return dynamometry.compute({
            "lado_acometido": "esquerdo", "massa_corporal_kg": None, "plataforma": [],
            "medidas": [{"grupo_muscular": "Quadríceps", "acao": "concentrica", "velocidade_graus_s": 60,
                         "angulo_graus": None, "unidade": "Nm", "pico_direito": dir_, "pico_esquerdo": esq}],
        })

    linhas = comparacao.comparar_dinamometria(dyn(200, 160), dyn(200, 182))
    lsi = next(linha for linha in linhas if linha["item"].startswith("LSI"))
    assert (lsi["antes"], lsi["depois"], lsi["direcao"]) == (80.0, 91.0, "melhorou")
    assert lsi["criterio_retorno"] is True
    assert any(linha["item"].startswith("Pico esquerdo") and linha["direcao"] == "melhorou" for linha in linhas)


def test_imagem_compara_secoes_do_texto():
    a = "## 3. ACHADOS\n- Edema ósseo\n## 4. IMPRESSÃO\nContusão."
    d = "## 3. ACHADOS\n- Sem edema\n## 4. IMPRESSÃO\nContusão."
    r = comparacao.comparar("joelho", {"texto": a}, {"texto": d})
    assert r["linhas"] == []
    blocos = {b["secao"]: b for b in r["texto"]}
    assert not blocos["Achados"]["igual"] and blocos["Impressão"]["igual"]


def test_pares_disponiveis_so_tipos_com_dois_exames():
    analises = [
        {"id": "1", "tipo_exame": "laboratorial", "created_at": "2026-01-01"},
        {"id": "2", "tipo_exame": "laboratorial", "created_at": "2026-03-01"},
        {"id": "3", "tipo_exame": "joelho", "created_at": "2026-02-01"},
        {"id": "4", "tipo_exame": "correlacao", "created_at": "2026-02-01"},
        {"id": "5", "tipo_exame": "correlacao", "created_at": "2026-02-02"},
    ]
    pares = comparacao.pares_disponiveis(analises)
    assert list(pares) == ["laboratorial"] and [a["id"] for a in pares["laboratorial"]] == ["2", "1"]


def test_destaques_pioras_primeiro():
    antes = _lab(LDL=(121, None, 130), Ferritina=(18, 30, 300))
    depois = _lab(LDL=(153, None, 130), Ferritina=(55, 30, 300))
    r = comparacao.comparar("laboratorial", {"dados": antes}, {"dados": depois})
    assert [d["item"] for d in r["destaques"]] == ["LDL", "Ferritina"]
    assert r["resumo"]["piorou"] == 1 and r["resumo"]["melhorou"] == 1


# ── Rota ──────────────────────────────────────────────────────────────────────

@pytest.fixture()
def profissional(client, monkeypatch):
    monkeypatch.setattr(db, "buscar_sessao_cliente", lambda e: {"id": "cliente-a", "sessao_versao": 0})
    monkeypatch.setattr(db, "buscar_paciente",
                        lambda pid, cid: {"id": pid, "nome": "Maria"} if (pid, cid) == ("pac-1", "cliente-a") else None)
    monkeypatch.setattr(db, "registrar_acesso_prontuario", MagicMock())
    client.set_cookie(security.PREMIUM_COOKIE, security.generate_premium_token("pay_1", "a@example.com", 3600))
    return client


def test_rota_retorno_mostra_o_que_mudou(profissional, monkeypatch):
    analises = [
        {"id": "lab-1", "tipo_exame": "laboratorial", "created_at": "2026-01-10T10:00:00+00:00"},
        {"id": "lab-2", "tipo_exame": "laboratorial", "created_at": "2026-04-10T10:00:00+00:00"},
    ]
    dados = {"lab-1": _lab(LDL=(121, None, 130)), "lab-2": _lab(LDL=(153, None, 130))}
    monkeypatch.setattr(db, "listar_analises_paciente", lambda pid, cid: analises)
    monkeypatch.setattr(db, "buscar_analise",
                        lambda aid, cid=None, include_all=False: next(dict(a) for a in analises if a["id"] == aid))
    monkeypatch.setattr(db, "buscar_dados_estruturados", lambda aid, cid=None, include_all=False: dados[aid])

    html = profissional.get("/pacientes/pac-1/retorno").get_data(as_text=True)
    assert "121 mg/dL" in html and "153 mg/dL" in html and "Piorou" in html

    # Escolher os exames invertidos não inverte a linha do tempo.
    html = profissional.get("/pacientes/pac-1/retorno?antes=lab-2&depois=lab-1").get_data(as_text=True)
    assert html.index("121 mg/dL") < html.index("153 mg/dL")


def test_rota_retorno_sem_par_volta_ao_prontuario(profissional, monkeypatch):
    monkeypatch.setattr(db, "listar_analises_paciente", lambda pid, cid: [{"id": "x", "tipo_exame": "joelho"}])
    resp = profissional.get("/pacientes/pac-1/retorno")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pacientes/pac-1")


def test_rota_retorno_de_paciente_alheio_404(profissional):
    assert profissional.get("/pacientes/pac-de-outro/retorno").status_code == 404
