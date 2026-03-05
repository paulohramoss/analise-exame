"""
Gerencia imagens de referência (normais) e atlas em PDF para exames ortopédicos.

Ordem de prioridade:
1. PDFs de atlas em reference_data/docs/          (base de conhecimento global)
2. Imagens commitadas em reference_data/<tipo>/   (referências por região anatômica)
3. Download de URLs públicas como fallback
"""

import requests
from pathlib import Path

_THIS_DIR = Path(__file__).parent
REFERENCE_DATA_DIR = _THIS_DIR.parent / "reference_data"
DOCS_DIR = REFERENCE_DATA_DIR / "docs"

# Regiões anatômicas ortopédicas suportadas
EXAM_TYPE_KEYWORDS = {
    "joelho":       ["joelho", "knee", "tibial", "femoral", "patela", "ligamento cruzado",
                     "menisco", "lcl", "lcm", "lca", "lcp"],
    "coluna":       ["coluna", "lombar", "cervical", "toracica", "torácica", "spine",
                     "vertebr", "disco", "hernia de disco", "sacro", "coccix", "l1", "l2",
                     "l3", "l4", "l5", "s1", "c1", "c2", "c3", "c4", "c5", "c6", "c7"],
    "ombro":        ["ombro", "shoulder", "manguito", "rotador", "acromioclavicular",
                     "glenoumeral", "umero", "clavícula", "clavicula", "escapula", "scapula"],
    "quadril":      ["quadril", "hip", "fêmur", "femur", "acetábulo", "acetabulo",
                     "ilíaco", "iliaco", "sacroilíaca", "sacroiliaca"],
    "pe_tornozelo": ["pé", "pe", "tornozelo", "ankle", "foot", "calcâneo", "calcaneo",
                     "tálus", "talus", "metatarso", "hallux", "plantar"],
    "mao_punho":    ["mão", "mao", "punho", "wrist", "hand", "carpo", "metacarpo",
                     "falange", "navicular", "escafoide"],
    "cotovelo":     ["cotovelo", "elbow", "úmero distal", "radio proximal", "ulna",
                     "epicondilo", "olecrano"],
}

FALLBACK_URLS = {
    "joelho": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9e/MRI_of_human_knee.jpg/800px-MRI_of_human_knee.jpg",
    ],
    "coluna": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/96/Vertebral_column_lateral_diagram.png/400px-Vertebral_column_lateral_diagram.png",
    ],
    "ombro": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cd/Slide1bgshoulder.JPG/800px-Slide1bgshoulder.JPG",
    ],
    "quadril": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/38/Pelvis_diagram.svg/800px-Pelvis_diagram.svg.png",
    ],
}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_PDF_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def detect_exam_type(filename: str, user_description: str = "") -> str:
    """Detecta a região anatômica ortopédica pelo nome do arquivo ou descrição."""
    combined = (filename + " " + user_description).lower()
    for exam_type, keywords in EXAM_TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return exam_type
    return "geral"


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
            print(f"Aviso: {pdf_path.name} excede 20 MB ({len(data)//1024//1024} MB) — ignorado.")
            return []
        print(f"Atlas carregado: {pdf_path.name} ({len(data)//1024} KB)")
        return [(data, "application/pdf")]
    except Exception as e:
        print(f"Erro ao carregar PDF {pdf_path.name}: {e}")
        return []


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
            ext = img_path.suffix.lower().lstrip(".")
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
            results.append((data, mime))
        except Exception as e:
            print(f"Aviso: erro ao ler {img_path.name}: {e}")

    return results


def _download_fallback(exam_type: str) -> list[tuple[bytes, str]]:
    """Baixa imagens de referência da internet como fallback. Cache em /tmp."""
    key = exam_type if exam_type in FALLBACK_URLS else "joelho"
    urls = FALLBACK_URLS[key]
    cache_dir = Path("/tmp/reference_data") / exam_type
    cache_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, url in enumerate(urls[:2]):
        cached_path = cache_dir / f"ref_{i}.jpg"

        if cached_path.exists():
            try:
                results.append((cached_path.read_bytes(), "image/jpeg"))
                continue
            except Exception:
                pass

        try:
            resp = requests.get(url, headers={"User-Agent": "EHealthOrtopedia/1.0"}, timeout=15)
            if resp.status_code == 200:
                cached_path.write_bytes(resp.content)
                results.append((resp.content, "image/jpeg"))
        except Exception as e:
            print(f"Aviso: não foi possível baixar referência de fallback: {e}")

    return results


def get_reference_images_as_bytes(exam_type: str) -> list[tuple[bytes, str]]:
    """
    Retorna imagens de referência como (bytes, mime_type) para envio ao Gemini.

    Prioridade:
    1. Imagens em reference_data/<exam_type>/ no repositório
    2. Download de URL pública como fallback
    """
    local_dir = REFERENCE_DATA_DIR / exam_type
    images = _load_images_from_dir(local_dir)

    if not images and exam_type != "geral":
        images = _load_images_from_dir(REFERENCE_DATA_DIR / "geral")

    if not images:
        images = _download_fallback(exam_type)

    return images
