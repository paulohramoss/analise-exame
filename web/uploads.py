"""Recebimento das imagens de exame (inclui conversão de DICOM para JPEG)."""

import base64
import importlib
import logging
import os
import tempfile
import uuid
from pathlib import Path

from flask import g, request
from PIL import Image
from werkzeug.utils import secure_filename

from web.security import hash_sensitive_value

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "analise_exame_uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "dcm"}
MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20MB
MAX_IMAGES_PER_ANALYSIS = 5
# Acesso admin (/admin/entrar) é usado para testes internos com séries DICOM
# inteiras, onde 5 imagens não cobrem os planos relevantes.
MAX_IMAGES_PER_ANALYSIS_ADMIN = int(os.environ.get("MAX_IMAGES_PER_ANALYSIS_ADMIN", "20"))

_MIME_BY_EXTENSION = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def max_images_for_request() -> int:
    """Limite de imagens por análise; admin interno recebe o teto ampliado."""
    if g.get("is_admin"):
        return MAX_IMAGES_PER_ANALYSIS_ADMIN
    return MAX_IMAGES_PER_ANALYSIS


def files_from_request():
    """Arquivos enviados em "exam_images" / "exam_images[]", ou no campo legado "exam_image"."""
    files = request.files.getlist("exam_images") or request.files.getlist("exam_images[]")
    if not files or all(f.filename == "" for f in files):
        legacy = request.files.get("exam_image")
        files = [legacy] if legacy and legacy.filename != "" else []
    return [f for f in files if f and f.filename != ""]


# ── DICOM ─────────────────────────────────────────────────────────────────────

def _dicom_value(ds, name: str) -> str:
    value = getattr(ds, name, "")
    if value is None:
        return ""
    return " ".join(str(value).split())[:180]


def _dicom_first_number(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        if isinstance(value, (list, tuple)) or hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            if len(value) == 0:
                return default
            value = value[0]
        return float(value)
    except Exception:
        return default


def _extract_dicom_metadata(ds) -> dict:
    rows = getattr(ds, "Rows", None)
    cols = getattr(ds, "Columns", None)
    metadata = {
        "modality": _dicom_value(ds, "Modality"),
        "body_part": _dicom_value(ds, "BodyPartExamined"),
        "study_description": _dicom_value(ds, "StudyDescription"),
        "series_description": _dicom_value(ds, "SeriesDescription"),
        "protocol_name": _dicom_value(ds, "ProtocolName"),
        "rows": int(rows) if rows else None,
        "columns": int(cols) if cols else None,
        "patient_id_hash": hash_sensitive_value(_dicom_value(ds, "PatientID")),
        "study_uid_hash": hash_sensitive_value(_dicom_value(ds, "StudyInstanceUID")),
        "series_uid_hash": hash_sensitive_value(_dicom_value(ds, "SeriesInstanceUID")),
        "sop_uid_hash": hash_sensitive_value(_dicom_value(ds, "SOPInstanceUID")),
    }
    return {key: value for key, value in metadata.items() if value not in {"", None}}


def _normalize_dicom_pixels(ds, pixel_array):
    np = importlib.import_module("numpy")
    array = pixel_array

    if array.ndim == 4:
        array = array[0]
    elif array.ndim == 3 and array.shape[-1] not in (3, 4):
        array = array[0]

    if array.ndim == 3 and array.shape[-1] in (3, 4):
        image = array
        if image.dtype != np.uint8:
            low, high = np.percentile(image.astype("float32"), [1, 99])
            if high <= low:
                high = low + 1
            image = np.clip((image.astype("float32") - low) * 255.0 / (high - low), 0, 255)
        return image.astype("uint8")

    image = array.astype("float32")
    slope = _dicom_first_number(getattr(ds, "RescaleSlope", None), 1.0) or 1.0
    intercept = _dicom_first_number(getattr(ds, "RescaleIntercept", None), 0.0) or 0.0
    image = image * slope + intercept

    center = _dicom_first_number(getattr(ds, "WindowCenter", None))
    width = _dicom_first_number(getattr(ds, "WindowWidth", None))
    if center is not None and width and width > 0:
        low = center - width / 2
        high = center + width / 2
    else:
        low, high = np.percentile(image, [1, 99])
        if high <= low:
            high = low + 1

    image = np.clip((image - low) * 255.0 / (high - low), 0, 255).astype("uint8")
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        image = 255 - image
    return image


def _convert_dicom_to_jpeg(filepath: Path) -> tuple[Path, str, str, dict] | None:
    try:
        pydicom = importlib.import_module("pydicom")
        ds = pydicom.dcmread(str(filepath), force=True)
        normalized = _normalize_dicom_pixels(ds, ds.pixel_array)
        image = Image.fromarray(normalized)
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        converted_path = filepath.with_suffix(".jpg")
        image.save(str(converted_path), format="JPEG", quality=92, optimize=True)
        image_b64 = base64.b64encode(converted_path.read_bytes()).decode("utf-8")
        return converted_path, image_b64, "image/jpeg", _extract_dicom_metadata(ds)
    except Exception:
        logger.exception("Falha ao converter DICOM %s", filepath.name)
        return None


def _dicom_context(upload_items: list[dict]) -> str:
    parts = []
    for item in upload_items:
        metadata = item.get("dicom_metadata") or {}
        if not metadata:
            continue
        values = []
        if metadata.get("modality"):
            values.append(f"Modalidade DICOM: {metadata['modality']}")
        if metadata.get("body_part"):
            values.append(f"Região DICOM: {metadata['body_part']}")
        if metadata.get("study_description"):
            values.append(f"Estudo: {metadata['study_description']}")
        if metadata.get("series_description"):
            values.append(f"Série: {metadata['series_description']}")
        if values:
            parts.append("; ".join(values))
    return " | ".join(parts)


def description_with_dicom_context(user_description: str, upload_items: list[dict]) -> str:
    dicom_context = _dicom_context(upload_items)
    if not dicom_context:
        return user_description
    if user_description:
        return f"{user_description} | {dicom_context}"
    return dicom_context


# ── Upload ────────────────────────────────────────────────────────────────────

def save_upload_file(file) -> dict | None:
    """
    Salva um arquivo de upload em disco.
    Retorna metadados da imagem processada ou None se inválido.
    """
    if not file or file.filename == "" or not allowed_file(file.filename):
        return None

    original_name = secure_filename(file.filename)
    filepath = UPLOAD_FOLDER / f"{uuid.uuid4().hex}_{original_name}"
    file.save(str(filepath))

    if filepath.suffix.lower() == ".dcm":
        dicom_result = _convert_dicom_to_jpeg(filepath)
        if not dicom_result:
            filepath.unlink(missing_ok=True)
            return None
        converted_path, image_b64, image_mime, dicom_metadata = dicom_result
        return {
            "filepath": converted_path,
            "display_b64": image_b64,
            "display_mime": image_mime,
            "storage_mime": "application/dicom",
            "source": "dicom",
            "original_name": original_name,
            "original_path": filepath,
            "dicom_metadata": dicom_metadata,
        }

    image_mime = _MIME_BY_EXTENSION.get(filepath.suffix.lower().lstrip("."), "image/jpeg")
    return {
        "filepath": filepath,
        "display_b64": base64.b64encode(filepath.read_bytes()).decode("utf-8"),
        "display_mime": image_mime,
        "storage_mime": image_mime,
        "source": "upload",
        "original_name": original_name,
        "original_path": filepath,
        "dicom_metadata": {},
    }


def read_upload_bytes(upload_items: list[dict]) -> tuple[list[tuple[bytes, str]], list[dict]]:
    """Lê os bytes de cada upload (para análise/fila) e os metadados de persistência.

    Usa sempre a imagem já convertida ("filepath"): para DICOM o arquivo original
    (.dcm) não é uma imagem que os modelos consigam decodificar.
    """
    images = []
    image_meta = []
    for upload_item in upload_items:
        images.append((Path(upload_item["filepath"]).read_bytes(), upload_item["display_mime"]))
        image_meta.append({
            "origem": upload_item.get("source") or "upload",
            "arquivo_original": upload_item.get("original_name") or "",
            "dicom_metadata": upload_item.get("dicom_metadata") or None,
        })
    return images, image_meta


def cleanup_upload_files(upload_items: list[dict]) -> None:
    paths = {
        str(upload_item[key])
        for upload_item in upload_items
        for key in ("filepath", "original_path")
        if upload_item.get(key)
    }
    for fp in paths:
        try:
            Path(fp).unlink(missing_ok=True)
        except OSError:
            pass
