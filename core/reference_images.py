"""
Gerencia imagens de referência (normais) para comparação com exames.

Ordem de prioridade:
1. Imagens commitadas no repositório em reference_data/<tipo_exame>/
2. Download de URLs públicas como fallback (cache em /tmp/reference_data)
"""

import requests
import os
from pathlib import Path

# Diretório de referências commitadas no repo (disponível no Vercel e localmente)
_THIS_DIR = Path(__file__).parent
REFERENCE_DATA_DIR = _THIS_DIR.parent / "reference_data"

# URLs de fallback — usadas apenas se não houver imagens locais no repo
REFERENCE_URLS = {
    "ressonancia_cerebro": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Lateral_head_on_MRI_edit.jpg/800px-Lateral_head_on_MRI_edit.jpg",
    ],
    "ressonancia_joelho": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9e/MRI_of_human_knee.jpg/800px-MRI_of_human_knee.jpg",
    ],
    "raio_x_torax": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7e/Normal_posteroanterior_%28PA%29_chest_radiograph_%28X-ray%29.jpg/800px-Normal_posteroanterior_%28PA%29_chest_radiograph_%28X-ray%29.jpg",
    ],
    "ressonancia_coluna": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/96/Vertebral_column_lateral_diagram.png/400px-Vertebral_column_lateral_diagram.png",
    ],
    "tomografia_cranio": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6c/Computed_tomography_of_human_brain_-_large.png/800px-Computed_tomography_of_human_brain_-_large.png",
    ],
    "geral": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Lateral_head_on_MRI_edit.jpg/800px-Lateral_head_on_MRI_edit.jpg",
    ],
}

EXAM_TYPE_KEYWORDS = {
    "ressonancia_cerebro": ["cerebro", "cranio", "brain", "mri head", "ressonancia cranio"],
    "ressonancia_joelho": ["joelho", "knee", "tibial", "femoral"],
    "raio_x_torax": ["torax", "pulm", "chest", "xray", "raio-x", "radiografia"],
    "ressonancia_coluna": ["coluna", "lombar", "cervical", "spine", "vertebr"],
    "tomografia_cranio": ["tomografia", "ct scan", "tac"],
    "ultrassonografia": ["ultrassom", "ultrasson", "ecografia", "doppler"],
    "mamografia": ["mamografia", "mama", "mamary", "breast"],
}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def detect_exam_type(filename: str, user_description: str = "") -> str:
    """Detecta o tipo de exame pelo nome do arquivo ou descrição."""
    combined = (filename + " " + user_description).lower()
    for exam_type, keywords in EXAM_TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return exam_type
    return "geral"


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
            with open(img_path, "rb") as f:
                data = f.read()
            ext = img_path.suffix.lower().lstrip(".")
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
            results.append((data, mime))
        except Exception as e:
            print(f"Aviso: erro ao ler {img_path.name}: {e}")

    return results


def _download_fallback(exam_type: str) -> list[tuple[bytes, str]]:
    """Baixa imagens de referência da internet como fallback. Cache em /tmp."""
    urls = REFERENCE_URLS.get(exam_type, REFERENCE_URLS["geral"])
    cache_dir = Path("/tmp/reference_data") / exam_type
    cache_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, url in enumerate(urls[:2]):
        cached_path = cache_dir / f"ref_{i}.jpg"

        if cached_path.exists():
            try:
                with open(cached_path, "rb") as f:
                    results.append((f.read(), "image/jpeg"))
                continue
            except Exception:
                pass

        try:
            resp = requests.get(url, headers={"User-Agent": "ThreeHealth/1.0"}, timeout=15)
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
    # 1. Tenta carregar do repositório
    local_dir = REFERENCE_DATA_DIR / exam_type
    images = _load_images_from_dir(local_dir)

    # Fallback para "geral" se o tipo específico não tiver imagens
    if not images and exam_type != "geral":
        images = _load_images_from_dir(REFERENCE_DATA_DIR / "geral")

    # 2. Se não houver imagens locais, baixa como fallback
    if not images:
        images = _download_fallback(exam_type)

    return images
