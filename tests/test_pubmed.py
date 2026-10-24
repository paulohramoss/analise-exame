"""
Referências do PubMed ao final do laudo: lesões tiradas do laudo, consultas por
foco (diagnóstico, tratamento, lesões associadas), leitura das respostas do
NCBI (simuladas, sem rede), reordenação e a seção em markdown.
"""

import pytest

from core import analyzer, pubmed
from evals import scoring

LAUDO = """## 1. IDENTIFICAÇÃO DO EXAME
- Modalidade: RM do joelho direito, plano sagital
## 2. ANÁLISE ESTRUTURAL — COMPARAÇÃO COM PADRÃO NORMAL
- Tendão patelar e ligamento colateral medial de aspecto normal. Cartilagem preservada.
## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
- **[FORTE]** Rotura completa do ligamento cruzado anterior (LCA), terço médio.
- **[MODERADO]** Rotura do corno posterior do menisco medial.
> **Achados descartados por evidência insuficiente:** possível fratura subcondral descartada por artefato.
## 4. IMPRESSÃO DIAGNÓSTICA
- Rotura completa do LCA — confirmado pela imagem
- Rotura meniscal medial — provável
## 5. IMPACTO NA ESTRUTURA FÍSICA DO PACIENTE
- Hérnia de disco não se aplica (texto fora das seções 3 e 4).
"""


def _summary(pmid, title, pubtype=("Journal Article", "Systematic Review")):
    return {"uid": pmid, "title": title, "authors": [{"name": "Autor A", "authtype": "Author"}],
            "source": "Revista", "pubdate": "2024 Jan", "articleids": [{"idtype": "doi", "value": f"10.1/{pmid}"}],
            "pubtype": list(pubtype)}


# Títulos reais devolvidos pelo PubMed (out/2026) para as buscas deste laudo.
ARTIGOS = {
    "1001": _summary("1001", "MRI diagnosis of anterior cruciate ligament tears: a meta-analysis."),
    "1002": _summary("1002", "American Academy of Orthopaedic Surgeons Clinical Practice Guideline Summary: "
                             "Management of Anterior Cruciate Ligament Injuries."),
    "1003": _summary("1003", "Risk of Secondary Injury in Younger Athletes After Anterior Cruciate Ligament "
                             "Reconstruction: A Systematic Review and Meta-analysis."),
    "2001": _summary("2001", "MRI Diagnosis of Meniscus Tears in the Knee: An Updated Systematic Review and Meta-analysis."),
    "2002": _summary("2002", "Inside-Out or Outside-In Suturing Should Not Be Considered the Standard Repair Method for "
                             "Radial Tears: A Systematic Review and Meta-Analysis of Biomechanical Studies."),
    "2003": _summary("2003", "Arthroscopic surgery for degenerative knee arthritis and meniscal tears: a clinical "
                             "practice guideline.", ("Practice Guideline",)),
    "3001": _summary("3001", "Rehabilitation after anterior cruciate ligament and meniscal injuries: a best-evidence "
                             "synthesis of systematic reviews for the OPTIKNEE consensus."),
}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture()
def ncbi(monkeypatch):
    """Simula o E-utilities. `respostas` dá os ids de cada esearch, na ordem das chamadas."""
    monkeypatch.setenv("PUBMED_REFERENCES_ENABLED", "1")
    monkeypatch.setenv("PUBMED_MIN_INTERVAL_SECONDS", "0")
    pubmed._cache.clear()
    calls = {"search": [], "summary": [], "respostas": [
        ["1001"],                    # LCA — diagnóstico
        ["1003", "1002"],            # LCA — tratamento
        ["2001"],                    # menisco — diagnóstico
        ["2002", "2003"],            # menisco — tratamento
        ["3001"],                    # LCA + menisco
    ]}

    def _get(url, params, timeout):
        if url.endswith("esearch.fcgi"):
            calls["search"].append(params["term"])
            return _Resp({"esearchresult": {"idlist": calls["respostas"].pop(0) if calls["respostas"] else []}})
        calls["summary"].append(params["id"])
        return _Resp({"result": {p: ARTIGOS[p] for p in params["id"].split(",") if p in ARTIGOS}})

    monkeypatch.setattr(pubmed.requests, "get", _get)
    return calls


def test_lesoes_vem_dos_achados_e_impressao_sem_os_descartados():
    labels = [label for label, _ in pubmed.detect_topics(LAUDO)]
    # fratura (descartada), cartilagem e colateral (seção 2) e hérnia (seção 5) ficam de fora
    assert labels == ["Lesão do ligamento cruzado anterior", "Lesão meniscal"]


def test_lesao_de_estrutura_exige_termo_de_lesao_no_titulo():
    [(_, lca), _] = pubmed.detect_topics(LAUDO)
    assert '"anterior cruciate ligament"[ti]' in lca and "tear[ti]" in lca and "rupture[ti]" in lca


def test_modalidade_vem_da_identificacao_do_exame():
    assert "magnetic resonance imaging" in pubmed.detect_modality(LAUDO)[0]
    assert "radiograph" in pubmed.detect_modality("## 1. IDENTIFICAÇÃO\n- Raio-X AP do joelho")[0]
    assert pubmed.detect_modality("## 1. IDENTIFICAÇÃO\n- sem modalidade") is None


def test_buscas_por_foco_diagnostico_tratamento_e_associadas():
    planos = pubmed.build_queries(LAUDO, "joelho")
    assert [(p["tema"], p["foco"]) for p in planos] == [
        ("Lesão do ligamento cruzado anterior", "Diagnóstico"),
        ("Lesão do ligamento cruzado anterior", "Tratamento"),
        ("Lesão meniscal", "Diagnóstico"),
        ("Lesão meniscal", "Tratamento"),
        ("Lesão do ligamento cruzado anterior + lesão meniscal", "Lesões associadas"),
    ]
    diagnostico, tratamento, _, _, associadas = (p["consultas"][0] for p in planos)
    assert "MRI[tiab]" in diagnostico and "treatment[ti]" not in diagnostico
    assert "treatment[ti]" in tratamento and "rehabilitation[ti]" in tratamento and "MRI[tiab]" not in tratamento
    assert '"anterior cruciate ligament"[ti]' in associadas and "meniscal[ti]" in associadas
    for query in (diagnostico, tratamento, associadas):
        assert "NOT case reports[pt]" in query and "english[la]" in query and "humans[mh]" in query
    estrita, ampla = planos[0]["consultas"]
    assert "OR review[pt]" not in estrita and "OR review[pt]" in ampla


def test_uma_lesao_so_nao_tem_busca_de_associadas():
    laudo = LAUDO.replace("menisco medial", "tendão").replace("meniscal", "tendínea")
    assert [p["foco"] for p in pubmed.build_queries(laudo, "joelho")] == ["Diagnóstico", "Tratamento"]


def test_sem_lesao_reconhecida_busca_pela_regiao():
    planos = pubmed.build_queries("## 1. IDENTIFICAÇÃO\n- RM do ombro\n## 3. ACHADOS\n- nada", "ombro")
    assert planos[0]["tema"] == "Exames de imagem: ombro" and "shoulder[ti]" in planos[0]["consultas"][0]


def test_nenhum_dado_do_paciente_vai_para_o_pubmed(ncbi):
    pubmed.find_references(LAUDO, "joelho", description="Paciente Maria, 45 anos, dor no joelho após queda")
    assert ncbi["search"] and all("Maria" not in q and "queda" not in q for q in ncbi["search"])


def test_referencias_de_diagnostico_e_tratamento_por_lesao(ncbi):
    grupos = pubmed.find_references(LAUDO, "joelho")
    resumo = [(g["tema"], [(a["foco"], a["pmid"]) for a in g["artigos"]]) for g in grupos]
    assert resumo == [
        ("Lesão do ligamento cruzado anterior", [("Diagnóstico", "1001"), ("Tratamento", "1002")]),  # diretriz AAOS
        ("Lesão meniscal", [("Diagnóstico", "2001"), ("Tratamento", "2003")]),  # biomecânica fica para trás
        ("Lesão do ligamento cruzado anterior + lesão meniscal", [("Lesões associadas", "3001")]),
    ]
    assert len(ncbi["summary"]) == 1  # um esummary para todos os candidatos


def test_cai_para_revisoes_quando_nao_ha_revisao_sistematica(ncbi):
    ncbi["respostas"] = [[], ["1001"]]  # LCA diagnóstico: estrita vazia, ampla com 1; o resto sem resultado
    [grupo] = pubmed.find_references(LAUDO, "joelho")
    assert [a["pmid"] for a in grupo["artigos"]] == ["1001"]
    assert "OR review[pt]" in ncbi["search"][1]


def test_mesmo_artigo_nao_se_repete(ncbi):
    ncbi["respostas"] = [["1001"], ["1001", "1002"]]
    [grupo] = pubmed.find_references(LAUDO, "joelho")
    assert [a["pmid"] for a in grupo["artigos"]] == ["1001", "1002"]


def test_falha_de_rede_devolve_lista_vazia(monkeypatch):
    monkeypatch.setenv("PUBMED_REFERENCES_ENABLED", "1")
    monkeypatch.setenv("PUBMED_MIN_INTERVAL_SECONDS", "0")
    pubmed._cache.clear()

    def _boom(*args, **kwargs):
        raise TimeoutError("NCBI fora do ar")

    monkeypatch.setattr(pubmed.requests, "get", _boom)
    assert pubmed.find_references(LAUDO, "joelho") == []


def test_desligado_nao_busca(monkeypatch):
    monkeypatch.setenv("PUBMED_REFERENCES_ENABLED", "0")
    monkeypatch.setattr(pubmed.requests, "get", lambda *a, **k: pytest.fail("não deveria buscar"))
    assert pubmed.find_references(LAUDO, "joelho") == []


def test_reordena_diagnostico_pela_modalidade():
    """Ordem real do PubMed para 'menisco + RM' (out/2026)."""
    candidatos = [
        {"pmid": "a", "titulo": "Management of traumatic meniscus tears: the 2019 ESSKA meniscus consensus.",
         "tipos": ["Journal Article", "Review"]},
        {"pmid": "b", "titulo": "Radiographic evaluation of key anatomical markers in discoid lateral meniscus: "
         "a systematic review and meta-analysis.", "tipos": ["Systematic Review", "Meta-Analysis"]},
        {"pmid": "c", "titulo": "Standardized Knee Meniscus MRI Reporting: An Interdisciplinary Delphi Consensus.",
         "tipos": ["Journal Article", "Review"]},
        {"pmid": "d", "titulo": "MRI Diagnosis of Meniscus Tears in the Knee: An Updated Systematic Review "
         "and Meta-analysis.", "tipos": ["Systematic Review", "Meta-Analysis"]},
    ]
    mri = pubmed.detect_modality(LAUDO)[2]
    assert [a["pmid"] for a in pubmed.rank_articles(candidatos, "Diagnóstico", mri)][:2] == ["d", "c"]
    # para tratamento, o consenso de manejo vem primeiro
    assert pubmed.rank_articles(candidatos, "Tratamento")[0]["pmid"] == "a"


def test_secao_formatada_com_foco_doi_e_pmid():
    artigo = {"pmid": "42048590", "titulo": "MRI *Diagnosis* of [Meniscus] Tears.", "autores": "Nguyen JC, et al",
              "revista": "Radiology", "ano": "2026", "doi": "10.1148/radiol.252288", "foco": "Diagnóstico"}
    sem_doi = dict(artigo, pmid="1", doi="", foco="Tratamento")
    secao = pubmed.format_section([{"tema": "Lesão meniscal", "artigos": [artigo, sem_doi]}])
    assert secao.startswith(pubmed.SECTION_TITLE)
    assert ("- Diagnóstico: Nguyen JC, et al. MRI Diagnosis of Meniscus Tears. Radiology. 2026. PMID 42048590. "
            "https://doi.org/10.1148/radiol.252288") in secao
    assert "- Tratamento: " in secao and "https://pubmed.ncbi.nlm.nih.gov/1/" in secao
    assert "et al." in secao and "et al.." not in secao
    assert "não confirmam isoladamente o achado" in secao
    assert pubmed.format_section([]) == ""


def test_autores_com_et_al_sem_ponto_duplo(ncbi):
    ARTIGOS["9999"] = dict(_summary("9999", "x"), authors=[{"name": n, "authtype": "Author"} for n in "ABCD"])
    try:
        artigo = pubmed._article_from_summary("9999", ARTIGOS["9999"])
    finally:
        del ARTIGOS["9999"]
    assert artigo["autores"] == "A, B, C, et al"


def test_analise_termina_com_as_referencias(monkeypatch):
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda *a, **k: object())
    monkeypatch.setattr(analyzer, "detect_exam_type_from_image", lambda *a, **k: "joelho")
    monkeypatch.setattr(analyzer, "_build_content_parts", lambda *a, **k: ([], 0))
    monkeypatch.setattr(analyzer, "get_reference_images_as_bytes", lambda t: [])
    monkeypatch.setattr(analyzer, "_produce_final_analysis", lambda *a, **k: (LAUDO, "m", []))
    grupos = [{"tema": "Lesão meniscal", "artigos": [{"pmid": "42048590", "titulo": "MRI Diagnosis", "autores": "Nguyen JC",
                                                     "revista": "Radiology", "ano": "2026", "doi": "10.1148/radiol.252288",
                                                     "foco": "Diagnóstico"}]}]
    monkeypatch.setattr(analyzer.pubmed, "find_references", lambda analysis, exam_type, description: grupos)

    result = analyzer.analyze_exam_from_bytes([(b"img-ref", "image/png")], "joelho.png", "chave")

    assert result["analysis"].startswith(LAUDO.rstrip())
    assert result["analysis"].rstrip().endswith("https://doi.org/10.1148/radiol.252288")
    assert result["references"] == grupos


def test_avaliacao_ignora_a_secao_de_referencias():
    laudo = LAUDO.replace("ligamento cruzado anterior (LCA)", "X").replace("LCA", "X") + "\n" + pubmed.format_section(
        [{"tema": "Lesão do ligamento cruzado anterior", "artigos": [
            {"pmid": "1", "titulo": "Rotura do ligamento cruzado anterior", "autores": "", "revista": "X", "ano": "2020", "doi": ""}]}])
    caso = {"expected_findings": [["ligamento cruzado anterior"]]}
    assert scoring.score_report(laudo, "joelho", caso)["found_findings"] == []
