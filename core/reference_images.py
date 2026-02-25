"""
Gerencia imagens de referência (normais) para comparação com exames.
Busca imagens de repositórios públicos de imagens médicas.
"""

import requests
import os
from pathlib import Path

# Referências de imagens médicas normais por tipo de exame
# Usando imagens de domínio público de repositórios médicos
REFERENCE_URLS = {
    "ressonancia_cerebro": [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/1/19/Cerebral_angiography%2C_arteria_vertebralis.jpg/800px-Cerebral_angiography%2C_arteria_vertebralis.jpg",
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
    "raio_x_torax": ["torax", "pulm", "chest", "xray", "raio-x"],
    "ressonancia_coluna": ["coluna", "lombar", "cervical", "spine", "vertebr"],
    "tomografia_cranio": ["tomografia", "ct scan", "tac"],
}


def detect_exam_type(filename: str, user_description: str = "") -> str:
    """Detecta o tipo de exame pelo nome do arquivo ou descrição."""
    combined = (filename + " " + user_description).lower()
    for exam_type, keywords in EXAM_TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return exam_type
    return "geral"


def fetch_reference_image(exam_type: str, save_dir: str = "reference_data") -> list[str]:
    """
    Busca imagens de referência normais para o tipo de exame.
    Retorna lista de caminhos locais das imagens baixadas.
    """
    urls = REFERENCE_URLS.get(exam_type, REFERENCE_URLS["geral"])
    os.makedirs(save_dir, exist_ok=True)
    saved_paths = []

    for i, url in enumerate(urls[:2]):  # Máximo 2 referências
        filename = f"{exam_type}_normal_{i}.jpg"
        filepath = os.path.join(save_dir, filename)

        if os.path.exists(filepath):
            saved_paths.append(filepath)
            continue

        try:
            headers = {"User-Agent": "MedicalExamAnalyzer/1.0"}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                with open(filepath, "wb") as f:
                    f.write(response.content)
                saved_paths.append(filepath)
        except Exception as e:
            print(f"Aviso: Não foi possível baixar imagem de referência: {e}")

    return saved_paths


def get_reference_images_as_bytes(exam_type: str) -> list[tuple[bytes, str]]:
    """
    Retorna imagens de referência como bytes para envio ao Gemini.
    Retorna lista de tuplas (bytes_da_imagem, mime_type).
    """
    paths = fetch_reference_image(exam_type)
    results = []

    for path in paths:
        try:
            with open(path, "rb") as f:
                image_bytes = f.read()
            results.append((image_bytes, "image/jpeg"))
        except Exception as e:
            print(f"Erro ao ler imagem de referência: {e}")

    return results
