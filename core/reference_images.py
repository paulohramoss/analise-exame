"""
Gerencia imagens de referência (normais) e atlas em PDF para exames ortopédicos.

Ordem de prioridade:
1. PDFs de atlas em reference_data/docs/          (base de conhecimento global)
2. Imagens commitadas em reference_data/<tipo>/   (referências por região anatômica;
   <tipo> é exatamente a chave de EXAM_TYPE_KEYWORDS: joelho, coluna, ombro…)
3. Download de URLs públicas como fallback — só para regiões com uma imagem
   de exame normal conhecida. Sem referência adequada, a análise segue sem
   comparação (melhor do que comparar uma mão com uma RM de joelho).
"""

import io
import logging
import re
import tempfile
import unicodedata
from pathlib import Path

import requests
from PIL import Image

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).parent
REFERENCE_DATA_DIR = _THIS_DIR.parent / "reference_data"
DOCS_DIR = REFERENCE_DATA_DIR / "docs"

# Regiões anatômicas ortopédicas suportadas.
#
# Termos casam como palavra inteira, já sem acento e em minúsculas (ver
# _normalize). Um termo terminado em "*" casa como prefixo de palavra
# ("vertebr*" → vértebra, vertebral). Antes a busca era por substring: "pe"
# casava com "pequeno", "hip" com "hipótese", "c1" com qualquer coisa.
#
# Cada região soma 1 ponto por termo encontrado e vence a de maior pontuação
# (empate: ordem deste dicionário). Termos que pertencem a duas regiões
# (fêmur, úmero, navicular) aparecem só em expressões que desambiguam.
EXAM_TYPE_KEYWORDS = {
    "joelho":       ["joelho", "knee", "patela", "patelar", "menisco*", "ligamento cruzado",
                     "lca", "lcp", "lcm", "lcl", "condilo femoral", "plato tibial",
                     "planalto tibial", "femur distal", "tibia proximal", "femorotibial",
                     "femoropatelar"],
    "coluna":       ["coluna", "lombar", "cervical", "toracica", "spine", "vertebr*",
                     "disco intervertebral", "hernia de disco", "discal", "sacro", "coccix",
                     "lombociatalgia", "cervicalgia", "lombalgia",
                     # Sem T1–T12: "T1"/"T2" são sequências de RM ("ponderada em T2").
                     "l1", "l2", "l3", "l4", "l5", "s1", "c1", "c2", "c3", "c4", "c5", "c6", "c7"],
    "ombro":        ["ombro", "shoulder", "manguito", "rotador", "supraespinhal",
                     "infraespinhal", "subescapular", "acromio*", "acromioclavicular",
                     "glenoumeral", "glenoide", "cabeca do umero", "cabeca umeral",
                     "clavicula", "escapula", "scapula"],
    "quadril":      ["quadril", "hip", "acetabulo", "acetabular", "coxofemoral",
                     "cabeca femoral", "cabeca do femur", "colo femoral", "colo do femur",
                     "trocanter*", "iliaco", "sacroiliaca", "pelve", "pelvis"],
    "pe_tornozelo": ["pe", "pes", "tornozelo", "ankle", "foot", "calcaneo", "talus", "talar",
                     "metatarso*", "hallux", "plantar", "fascia plantar", "tendao de aquiles",
                     "aquiles", "tibia distal", "fibula distal", "navicular do pe"],
    "mao_punho":    ["mao", "maos", "punho", "wrist", "hand", "carpo", "carpal", "metacarpo*",
                     "falange*", "escafoide", "radio distal", "dedo", "dedos", "polegar"],
    "cotovelo":     ["cotovelo", "elbow", "umero distal", "radio proximal", "cabeca do radio",
                     "ulna", "epicondil*", "olecrano"],
}

# Imagens públicas (Wikimedia Commons) de exame normal, por região. Só entram
# imagens de exame real: os diagramas que estavam aqui para coluna e quadril
# foram removidos — um desenho esquemático não serve de padrão de comparação.
FALLBACK_URLS = {
    "joelho": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9e/MRI_of_human_knee.jpg/800px-MRI_of_human_knee.jpg",
    ],
    "ombro": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cd/Slide1bgshoulder.JPG/800px-Slide1bgshoulder.JPG",
    ],
}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_PDF_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def _normalize(text: str) -> str:
    """Minúsculas, sem acento, e qualquer coisa que não seja letra/dígito vira espaço.

    O "_" também vira espaço: em "joelho_direito.jpg" o \\b do regex não separava
    "joelho" de "_direito" (o "_" conta como caractere de palavra).
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())


def _keyword_pattern(keyword: str) -> re.Pattern:
    is_prefix = keyword.endswith("*")
    term = _normalize(keyword.rstrip("*"))
    return re.compile(rf"\b{re.escape(term)}" + ("" if is_prefix else r"\b"))


_KEYWORD_PATTERNS = {
    exam_type: [_keyword_pattern(kw) for kw in keywords]
    for exam_type, keywords in EXAM_TYPE_KEYWORDS.items()
}


def detect_exam_type(filename: str, user_description: str = "") -> str:
    """Detecta a região anatômica ortopédica pelo nome do arquivo ou descrição."""
    combined = _normalize(f"{filename} {user_description}")
    best_type, best_score = "geral", 0
    for exam_type, patterns in _KEYWORD_PATTERNS.items():
        score = sum(1 for pattern in patterns if pattern.search(combined))
        if score > best_score:
            best_type, best_score = exam_type, score
    return best_type


def get_reference_pdfs() -> list[tuple[bytes, str]]:
    """
    Carrega PDFs de atlas/livros de reference_data/docs/.
    Retorna lista de (bytes, 'application/pdf'). Máximo 1 PDF por análise.
    """
    if not DOCS_DIR.exists():
        return []

    pdf_files = sorted(DOCS_DIR.glob("*.pdf"))
    if not pdf_files:
        return []

    pdf_path = pdf_files[0]
    try:
        data = pdf_path.read_bytes()
        if len(data) > _PDF_MAX_BYTES:
            logger.warning("%s excede 20 MB (%d MB) — ignorado.", pdf_path.name, len(data) // 1024 // 1024)
            return []
        logger.info("Atlas carregado: %s (%d KB)", pdf_path.name, len(data) // 1024)
        return [(data, "application/pdf")]
    except Exception:
        logger.exception("Erro ao carregar PDF %s", pdf_path.name)
        return []


def _image_mime(data: bytes) -> str | None:
    """MIME real da imagem pelo conteúdo (não pela extensão/URL), ou None se não for imagem."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
    except Exception:
        return None
    return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)


def _load_images_from_dir(directory: Path, max_images: int = 2) -> list[tuple[bytes, str]]:
    """Carrega imagens de um diretório local. Retorna lista de (bytes, mime_type)."""
    results = []
    if not directory.exists():
        return results

    image_files = sorted(
        f for f in directory.iterdir()
        if f.suffix.lower() in _IMAGE_EXTENSIONS
    )

    for img_path in image_files[:max_images]:
        try:
            data = img_path.read_bytes()
        except Exception:
            logger.warning("Erro ao ler %s", img_path.name, exc_info=True)
            continue
        mime = _image_mime(data)
        if mime:
            results.append((data, mime))
        else:
            logger.warning("Referência ignorada (não é imagem válida): %s", img_path.name)

    return results


def _download_fallback(exam_type: str) -> list[tuple[bytes, str]]:
    """Baixa imagens de referência da internet como fallback. Cache no diretório temporário do sistema.

    Só para regiões listadas em FALLBACK_URLS. Antes, regiões sem URL (mão,
    cotovelo, pé, geral) recebiam a RM de joelho como "referência normal".
    """
    urls = FALLBACK_URLS.get(exam_type)
    if not urls:
        return []
    cache_dir = Path(tempfile.gettempdir()) / "reference_data" / exam_type
    cache_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, url in enumerate(urls[:2]):
        cached_path = cache_dir / f"ref_{i}"

        data = None
        if cached_path.exists():
            try:
                data = cached_path.read_bytes()
            except Exception:
                data = None

        if data is None:
            try:
                resp = requests.get(url, headers={"User-Agent": "EHealthOrtopedia/1.0"}, timeout=15)
                if resp.status_code == 200:
                    data = resp.content
            except Exception:
                logger.warning("Não foi possível baixar referência de fallback: %s", url, exc_info=True)

        mime = _image_mime(data) if data else None
        if not mime:
            continue
        if not cached_path.exists():
            try:
                cached_path.write_bytes(data)
            except Exception:
                logger.warning("Não foi possível cachear referência em %s", cached_path, exc_info=True)
        results.append((data, mime))

    return results


def get_reference_images_as_bytes(exam_type: str) -> list[tuple[bytes, str]]:
    """
    Retorna imagens de referência como (bytes, mime_type) para envio ao Gemini.

    Prioridade:
    1. Imagens em reference_data/<exam_type>/ no repositório
    2. Download de URL pública da mesma região (se houver)

    reference_data/geral/ só é usada quando a região é "geral": uma imagem
    genérica não serve de padrão para uma região específica.
    """
    images = _load_images_from_dir(REFERENCE_DATA_DIR / exam_type)
    if not images:
        images = _download_fallback(exam_type)
    if not images:
        logger.info("Sem imagem de referência para %s — análise segue sem comparação visual.", exam_type)
    return images
