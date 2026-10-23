"""
Referências científicas do PubMed ao final do laudo.

Os artigos vêm de busca real no PubMed (E-utilities do NCBI), nunca do texto
gerado pela IA: modelo de linguagem inventa referência com título, autor e DOI
plausíveis. Aqui a IA não participa — as lesões saem do próprio laudo por
dicionário (lesão em português → termos em inglês no título do artigo), e só
entra artigo que o PubMed devolveu, com PMID e DOI. Nada do paciente sai para
o NCBI: as consultas são montadas só com termos do dicionário.

Para cada lesão dos achados [FORTE]/[MODERADO] e da impressão diagnóstica
(achados descartados ficam de fora), até três focos:
    Diagnóstico         lesão no título AND região AND modalidade do exame
    Tratamento          lesão no título AND região AND tratamento/manejo/cirurgia/reabilitação
    Lesões associadas   duas lesões do laudo no título (ex.: LCA + menisco)
Todas filtram por revisão sistemática, meta-análise ou diretriz (se não houver,
revisão), sem relatos de caso, em humanos, em inglês, dos últimos 15 anos. Os
candidatos são reordenados para o foco (ver rank_articles).

Falha de rede, timeout ou PubMed fora do ar → lista vazia, e o laudo sai sem
a seção. Resultados ficam em cache por 7 dias (as lesões se repetem muito).
"""

import logging
import os
import re
import threading
import time
from datetime import date

import requests

from core.ttl_cache import TTLCache

logger = logging.getLogger(__name__)

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TIMEOUT_SECONDS = float(os.environ.get("PUBMED_TIMEOUT_SECONDS", "12"))
_MAX_TOPICS = int(os.environ.get("PUBMED_MAX_TOPICS", "3"))
_YEARS = int(os.environ.get("PUBMED_YEARS", "15"))
_CANDIDATES = int(os.environ.get("PUBMED_CANDIDATES_PER_QUERY", "6"))
_cache = TTLCache(maxsize=1024, ttl_seconds=7 * 24 * 3600)

SECTION_TITLE = "## 7. REFERÊNCIAS CIENTÍFICAS (PubMed)"

# Lesão de estrutura (ligamento, menisco, tendão…): o título precisa falar da lesão.
_LESION = "(tear[ti] OR tears[ti] OR rupture[ti] OR injury[ti] OR injuries[ti] OR lesion[ti] OR lesions[ti])"

# (rótulo, padrão no laudo em português, termos no título, exige termo de lesão)
TOPICS: list[tuple[str, str, str, bool]] = [
    ("Lesão do ligamento cruzado anterior", r"cruzado anterior|\bLCA\b",
     '("anterior cruciate ligament"[ti] OR ACL[ti])', True),
    ("Lesão meniscal", r"menisc", "(meniscal[ti] OR meniscus[ti] OR menisci[ti])", True),
    ("Lesão do ligamento cruzado posterior", r"cruzado posterior|\bLCP\b",
     '("posterior cruciate ligament"[ti] OR PCL[ti])', True),
    ("Lesão de ligamento colateral", r"colateral (medial|lateral)|\bLC[ML]\b",
     '("collateral ligament"[ti] OR MCL[ti] OR LCL[ti])', True),
    ("Instabilidade patelar", r"instabilidade patelar|luxação (da )?patela|subluxação patelar",
     '("patellar instability"[ti] OR "patellar dislocation"[ti])', False),
    ("Lesão condral", r"condromalácia|lesão condral|defeito condral|osteocondral|cartilag",
     "(cartilage[ti] OR chondral[ti] OR chondromalacia[ti] OR osteochondral[ti])", True),
    ("Edema ósseo / contusão óssea", r"edema (ósseo|medular|subcondral)|contusão óssea",
     '("bone marrow edema"[ti] OR "bone marrow oedema"[ti] OR "bone bruise"[ti] OR "bone marrow lesion"[ti])', False),
    ("Osteonecrose", r"osteonecrose|necrose avascular|necrose asséptica", '(osteonecrosis[ti] OR "avascular necrosis"[ti])', False),
    ("Osteoartrite", r"osteoartr|artrose|gonartrose|coxartrose|kellgren", "(osteoarthritis[ti])", False),
    ("Fratura", r"fratura|traço de fratura", "(fracture[ti] OR fractures[ti])", False),
    ("Lesão do manguito rotador", r"manguito|supraespinal|infraespinal|subescapular",
     '("rotator cuff"[ti] OR supraspinatus[ti] OR subscapularis[ti])', True),
    ("Lesão labral", r"\blabr|lábio (glenoidal|acetabular)|\bSLAP\b|bankart",
     "(labral[ti] OR labrum[ti] OR SLAP[ti] OR Bankart[ti])", True),
    ("Impacto femoroacetabular", r"impacto femoroacetabular|\bcam\b|pincer", '("femoroacetabular impingement"[ti])', False),
    ("Impacto subacromial", r"impacto subacromial|síndrome do impacto",
     '("subacromial impingement"[ti] OR "shoulder impingement"[ti])', False),
    ("Lesão do tendão calcâneo (Aquiles)", r"aquiles|tendão calcâneo", "(achilles[ti])", True),
    ("Tendinopatia", r"tendinopatia|tendinose|tendinite", "(tendinopathy[ti] OR tendinosis[ti] OR tendinitis[ti])", False),
    ("Fasciopatia plantar", r"fasc(i|e)ite plantar|fáscia plantar", '("plantar fasciitis"[ti] OR "plantar fascia"[ti])', False),
    ("Lesão ligamentar do tornozelo", r"talofibular|calcaneofibular|entorse (de|do) tornozelo|sindesmose",
     '("ankle sprain"[ti] OR "lateral ankle ligament"[ti] OR talofibular[ti] OR syndesmosis[ti])', False),
    ("Epicondilopatia", r"epicondil", "(epicondylitis[ti] OR epicondylalgia[ti] OR epicondylopathy[ti])", False),
    ("Síndrome do túnel do carpo", r"túnel do carpo|nervo mediano", '("carpal tunnel"[ti])', False),
    ("Lesão da fibrocartilagem triangular", r"fibrocartilagem triangular|\bTFCC\b|\bCFCT\b",
     '("triangular fibrocartilage"[ti] OR TFCC[ti])', True),
    ("Hérnia de disco", r"hérnia|herniação|protrusão discal|extrusão discal|abaulamento discal",
     '("disc herniation"[ti] OR "disk herniation"[ti] OR "herniated disc"[ti] OR "herniated disk"[ti])', False),
    ("Degeneração discal", r"degeneração discal|discopatia|desidratação discal|pfirrmann|modic",
     '("disc degeneration"[ti] OR "disk degeneration"[ti] OR Modic[ti])', False),
    ("Estenose do canal", r"estenose", "(stenosis[ti])", False),
    ("Espondilolistese", r"espondilolist|espondilólise", "(spondylolisthesis[ti] OR spondylolysis[ti])", False),
    ("Bursite", r"bursite", "(bursitis[ti])", False),
    ("Cisto poplíteo (Baker)", r"baker|cisto poplíteo", '("popliteal cyst"[ti] OR "Baker cyst"[ti])', False),
]

REGIONS = {
    "joelho": ("joelho", "knee[tiab]"),
    "ombro": ("ombro", "shoulder[tiab]"),
    "coluna": ("coluna", "(spine[tiab] OR spinal[tiab] OR lumbar[tiab] OR cervical[tiab])"),
    "quadril": ("quadril", "hip[tiab]"),
    "pe_tornozelo": ("pé e tornozelo", "(ankle[tiab] OR foot[tiab])"),
    "mao_punho": ("mão e punho", "(wrist[tiab] OR hand[tiab])"),
    "cotovelo": ("cotovelo", "elbow[tiab]"),
}

# (padrão no laudo, filtro de modalidade, termo para a busca de fallback por região,
#  padrão no título do artigo que conta ponto na reordenação)
MODALITIES = [
    (r"\bRM\b|ressonância", '("magnetic resonance imaging"[mh] OR MRI[tiab])', "MRI[ti]",
     r"\bMRI?\b|magnetic resonance"),
    (r"\bTC\b|tomografia", '("tomography, x-ray computed"[mh] OR CT[tiab])', '("computed tomography"[ti] OR CT[ti])',
     r"\bCT\b|computed tomography"),
    (r"\bUS\b|ultrassom|ultrassonografia|ecografia", '("ultrasonography"[mh] OR ultrasound[tiab])',
     "(ultrasound[ti] OR ultrasonography[ti])", r"ultrasound|ultrasonograph|sonograph"),
    (r"raio[- ]?x|radiografia|\bRX\b", '("radiography"[mh] OR radiograph[tiab] OR radiographs[tiab] OR radiography[tiab])',
     "(radiograph[ti] OR radiographs[ti] OR radiography[ti])", r"radiograph|x-ray"),
]

_TREATMENT = (
    "(treatment[ti] OR management[ti] OR surgery[ti] OR surgical[ti] OR repair[ti] OR reconstruction[ti] "
    "OR rehabilitation[ti] OR nonoperative[ti] OR conservative[ti] OR therapy[ti])"
)
_STRONG_TYPES = "(systematic review[pt] OR meta-analysis[pt] OR guideline[pt] OR practice guideline[pt] OR consensus[ti])"
_ANY_REVIEW = "(systematic review[pt] OR meta-analysis[pt] OR guideline[pt] OR practice guideline[pt] OR review[pt])"

_STRONG_PUBTYPES = {"Systematic Review", "Meta-Analysis", "Guideline", "Practice Guideline", "Consensus Development Conference"}
_DIAGNOSTIC_TITLE = r"diagnos|imaging|accuracy|reporting|classification"
_TREATMENT_TITLE = r"treat|manag|surg|repair|reconstruct|rehabilitat|nonoperative|conservative|therap|outcome"
_GUIDELINE_TITLE = r"guideline|consensus|recommendation"
# Estudos fora da clínica do paciente: biomecânica, cadáver, animal.
_OFF_TOPIC_TITLE = r"biomechanic|cadaver|animal model|in vitro"

FOCO_DIAGNOSTICO = "Diagnóstico"
FOCO_TRATAMENTO = "Tratamento"
FOCO_ASSOCIADAS = "Lesões associadas"


def _relevant_text(analysis: str) -> str:
    """Achados e impressão diagnóstica (seções 3 e 4), sem os achados descartados.

    A seção 2 compara cada estrutura com o normal e citaria quase tudo; os
    descartados não sustentam o laudo e não devem ganhar referência.
    """
    match = re.search(r"##\s*3\..*?(?=##\s*5\.|\Z)", analysis or "", re.DOTALL)
    text = match.group(0) if match else (analysis or "")
    text = re.sub(r"(?im)^.*achados descartados.*$", "", text)
    return re.sub(r"(?im)^.*\[FRACO\].*$", "", text)


def detect_topics(analysis: str) -> list[tuple[str, str]]:
    """Lesões (rótulo, termos de busca) na ordem em que aparecem no laudo."""
    text = _relevant_text(analysis)
    found = []
    for label, pattern, terms, needs_lesion in TOPICS:
        hit = re.search(pattern, text, re.IGNORECASE)
        if hit:
            found.append((hit.start(), label, f"{terms} AND {_LESION}" if needs_lesion else terms))
    found.sort()
    return [(label, terms) for _, label, terms in found[:_MAX_TOPICS]]


def detect_modality(analysis: str, description: str = "") -> tuple[str, str, str] | None:
    """(filtro, termo de título, padrão de título) da modalidade, pela seção 1 do laudo ou descrição."""
    ident = re.search(r"##\s*1\..*?(?=##\s*2\.|\Z)", analysis or "", re.DOTALL)
    text = f"{ident.group(0) if ident else ''}\n{description or ''}"
    for pattern, mesh_filter, title_term, title_pattern in MODALITIES:
        if re.search(pattern, text, re.IGNORECASE):
            return mesh_filter, title_term, title_pattern
    return None


def _query(*parts: str, types: str) -> str:
    year = date.today().year
    return (
        " AND ".join(p for p in parts if p)
        + f" AND {types} NOT case reports[pt] AND humans[mh] AND english[la] AND {year - _YEARS}:{year}[dp]"
    )


def build_queries(analysis: str, exam_type: str, description: str = "") -> list[dict]:
    """Buscas do laudo: [{"tema", "foco", "consultas": [estrita, ampla]}], na ordem de exibição."""
    region = REGIONS.get(exam_type)
    region_term = region[1] if region else ""
    modality = detect_modality(analysis, description)
    topics = detect_topics(analysis)

    plans = []
    for label, terms in topics:
        diagnosis = [terms, region_term, modality[0] if modality else '("diagnostic imaging"[mh] OR imaging[tiab])']
        treatment = [terms, region_term, _TREATMENT]
        plans.append({"tema": label, "foco": FOCO_DIAGNOSTICO,
                      "consultas": [_query(*diagnosis, types=_STRONG_TYPES), _query(*diagnosis, types=_ANY_REVIEW)]})
        plans.append({"tema": label, "foco": FOCO_TRATAMENTO,
                      "consultas": [_query(*treatment, types=_STRONG_TYPES), _query(*treatment, types=_ANY_REVIEW)]})
    if len(topics) >= 2:
        (label_a, terms_a), (label_b, terms_b) = topics[:2]
        both = [terms_a.split(" AND ")[0], terms_b.split(" AND ")[0], region_term]
        plans.append({"tema": f"{label_a} + {label_b.lower()}", "foco": FOCO_ASSOCIADAS,
                      "consultas": [_query(*both, types=_STRONG_TYPES), _query(*both, types=_ANY_REVIEW)]})
    if not plans and region:
        region_ti = region[1].replace("[tiab]", "[ti]")
        modality_ti = modality[1] if modality else "(imaging[ti] OR MRI[ti] OR radiograph[ti])"
        plans.append({"tema": f"Exames de imagem: {region[0]}", "foco": FOCO_DIAGNOSTICO,
                      "consultas": [_query(region_ti, modality_ti, types=_STRONG_TYPES),
                                    _query(region_ti, modality_ti, types=_ANY_REVIEW)]})
    return plans


def rank_articles(articles: list[dict], foco: str, modality_title: str | None = None) -> list[dict]:
    """Reordena os candidatos (que chegam na ordem de relevância do PubMed) para o foco.

    Diagnóstico: +2 título da modalidade do exame, +1 título de diagnóstico/imagem.
    Tratamento e lesões associadas: +2 título de tratamento/manejo, +1 diretriz/consenso.
    Todos: +1 revisão sistemática/meta-análise/diretriz; −3 biomecânica/cadáver/animal.
    Empate: a relevância do PubMed.
    """
    def _score(item: tuple[int, dict]) -> tuple[int, int]:
        index, art = item
        title = art.get("titulo", "")
        score = 0
        if foco == FOCO_DIAGNOSTICO:
            if modality_title and re.search(modality_title, title, re.IGNORECASE):
                score += 2
            if re.search(_DIAGNOSTIC_TITLE, title, re.IGNORECASE):
                score += 1
        else:
            if re.search(_TREATMENT_TITLE, title, re.IGNORECASE):
                score += 2
            if re.search(_GUIDELINE_TITLE, title, re.IGNORECASE):
                score += 1
        if _STRONG_PUBTYPES & set(art.get("tipos") or []):
            score += 1
        if re.search(_OFF_TOPIC_TITLE, title, re.IGNORECASE):
            score -= 3
        return -score, index

    return [art for _, art in sorted(enumerate(articles), key=_score)]


def timeout_seconds() -> float:
    return _TIMEOUT_SECONDS


def _params(extra: dict) -> dict:
    params = {"db": "pubmed", "retmode": "json", "tool": "threehealth"}
    if os.environ.get("NCBI_EMAIL"):
        params["email"] = os.environ["NCBI_EMAIL"]
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    params.update(extra)
    return params


# O NCBI aceita 3 requisições/s sem chave (10/s com NCBI_API_KEY): espaça as chamadas.
_rate_lock = threading.Lock()
_last_request = [0.0]


def _get(endpoint: str, params: dict, deadline: float) -> dict:
    default = "0.11" if os.environ.get("NCBI_API_KEY") else "0.34"
    interval = float(os.environ.get("PUBMED_MIN_INTERVAL_SECONDS", default))
    with _rate_lock:
        wait = _last_request[0] + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
    resp = requests.get(f"{_EUTILS}/{endpoint}", params=_params(params), timeout=max(0.5, deadline - time.monotonic()))
    resp.raise_for_status()
    return resp.json()


def _esearch(query: str, deadline: float) -> list[str]:
    cached = _cache.get(("s", query))
    if cached is not None:
        return cached
    data = _get("esearch.fcgi", {"term": query, "retmax": _CANDIDATES, "sort": "relevance"}, deadline)
    ids = data.get("esearchresult", {}).get("idlist", [])
    _cache.set(("s", query), ids)
    return ids


def _esummary(pmids: list[str], deadline: float) -> dict:
    missing = [p for p in pmids if _cache.get(("a", p)) is None]
    if missing:
        result = _get("esummary.fcgi", {"id": ",".join(missing)}, deadline).get("result", {})
        for pmid in missing:
            if isinstance(result.get(pmid), dict) and result[pmid].get("title"):
                _cache.set(("a", pmid), _article_from_summary(pmid, result[pmid]))
    return {p: _cache.get(("a", p)) for p in pmids if _cache.get(("a", p))}


def _article_from_summary(pmid: str, item: dict) -> dict:
    authors = [a.get("name", "") for a in item.get("authors") or [] if a.get("authtype", "Author") == "Author"]
    authors = [a for a in authors if a]
    author_text = ", ".join(authors[:3]) + (", et al" if len(authors) > 3 else "")
    doi = next((i.get("value") for i in item.get("articleids") or [] if i.get("idtype") == "doi"), "")
    year = re.match(r"\d{4}", item.get("pubdate") or item.get("epubdate") or "")
    return {
        "pmid": pmid,
        "titulo": re.sub(r"<[^>]+>", "", item.get("title") or "").strip(),
        "autores": author_text,
        "revista": item.get("source") or item.get("fulljournalname") or "",
        "ano": year.group(0) if year else "",
        "doi": doi,
        "tipos": [t for t in item.get("pubtype") or [] if isinstance(t, str)],
    }


def find_references(analysis: str, exam_type: str, description: str = "") -> list[dict]:
    """[{"tema", "artigos": [{..., "foco"}]}] do PubMed para o laudo. Nunca levanta exceção.

    Um artigo por foco (diagnóstico, tratamento, lesões associadas), sem repetir
    artigo entre focos ou lesões.
    """
    if os.environ.get("PUBMED_REFERENCES_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return []
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    modality = detect_modality(analysis, description)
    try:
        candidates = []
        for plan in build_queries(analysis, exam_type, description):
            if time.monotonic() > deadline:
                break
            for query in plan["consultas"]:
                pmids = _esearch(query, deadline)
                if pmids:
                    candidates.append((plan, pmids))
                    break
        if not candidates:
            return []
        # Um esummary só, com os candidatos de todas as buscas.
        summaries = _esummary(list(dict.fromkeys(p for _, ids in candidates for p in ids)), deadline)

        groups: dict[str, list[dict]] = {}
        seen: set[str] = set()
        for plan, ids in candidates:
            ranked = rank_articles(
                [summaries[p] for p in ids if p in summaries], plan["foco"], modality[2] if modality else _DIAGNOSTIC_TITLE,
            )
            chosen = next((art for art in ranked if art["pmid"] not in seen), None)
            if chosen:
                seen.add(chosen["pmid"])
                groups.setdefault(plan["tema"], []).append({**chosen, "foco": plan["foco"]})
        return [{"tema": tema, "artigos": artigos} for tema, artigos in groups.items()]
    except Exception:
        logger.warning("Busca de referências no PubMed falhou; laudo segue sem a seção.", exc_info=True)
        return []


def _clean(text: str) -> str:
    """Texto vindo do PubMed sem sintaxe que o renderizador do laudo interpretaria."""
    return re.sub(r"[*_`#>\[\]]", "", " ".join(str(text or "").split()))


def format_section(groups: list[dict]) -> str:
    """Seção em markdown para o final do laudo (vazia se não há artigos)."""
    if not groups:
        return ""
    lines = [
        SECTION_TITLE,
        "",
        "> Artigos selecionados automaticamente no PubMed para as lesões descritas neste laudo: "
        "diagnóstico pelo exame de imagem e tratamento de cada lesão (revisões sistemáticas, "
        "meta-análises e diretrizes). Embasam a leitura; não confirmam isoladamente o achado deste paciente.",
    ]
    for group in groups:
        lines += ["", f"**{_clean(group['tema'])}**"]
        for art in group["artigos"]:
            foco = f"{art['foco']}: " if art.get("foco") else ""
            authors = f"{_clean(art['autores'])}. " if art.get("autores") else ""
            meta = ". ".join(p for p in [_clean(art["revista"]), art["ano"]] if p)
            link = f"https://doi.org/{art['doi']}" if art.get("doi") else f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}/"
            lines.append(f"- {foco}{authors}{_clean(art['titulo']).rstrip('.')}. {meta}. PMID {art['pmid']}. {link}")
    return "\n".join(lines)
