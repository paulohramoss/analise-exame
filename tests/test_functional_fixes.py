"""
Testes das correções funcionais: imagens de referência, cota do trial por IP,
armazenamento dos contadores, modelos configuráveis, redimensionamento de
imagem e contabilização de chamadas de IA abandonadas.
"""

import concurrent.futures
import io
import threading
from unittest.mock import MagicMock

from PIL import Image

import app as flask_app_module
from web import presentation
from core import analyzer, cost_tracking, reference_images


# ── Imagens de referência ─────────────────────────────────────────────────────

def _png_bytes(size=(64, 64)):
    buffer = io.BytesIO()
    Image.new("L", size, 100).save(buffer, format="PNG")
    return buffer.getvalue()


def test_reference_folders_match_exam_types():
    """As pastas tinham nomes antigos (ressonancia_joelho…) e nunca eram lidas."""
    for exam_type in [*reference_images.EXAM_TYPE_KEYWORDS, "geral"]:
        assert (reference_images.REFERENCE_DATA_DIR / exam_type).is_dir(), exam_type


def test_region_without_reference_gets_none_not_knee(monkeypatch):
    """Antes mão/cotovelo/pé recebiam a RM de joelho como 'referência normal'."""
    get = MagicMock()
    monkeypatch.setattr(reference_images.requests, "get", get)
    for exam_type in ("mao_punho", "cotovelo", "pe_tornozelo", "coluna", "quadril", "geral"):
        assert reference_images.get_reference_images_as_bytes(exam_type) == []
    get.assert_not_called()


def test_local_reference_mime_comes_from_content(tmp_path, monkeypatch):
    region = tmp_path / "joelho"
    region.mkdir()
    (region / "normal_01.jpg").write_bytes(_png_bytes())  # PNG com extensão .jpg
    (region / "lixo.jpg").write_bytes(b"nao-e-imagem")
    monkeypatch.setattr(reference_images, "REFERENCE_DATA_DIR", tmp_path)

    images = reference_images.get_reference_images_as_bytes("joelho")

    assert [mime for _, mime in images] == ["image/png"]


def test_downloaded_reference_mime_comes_from_content(tmp_path, monkeypatch):
    monkeypatch.setattr(reference_images.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        reference_images.requests, "get",
        lambda url, headers, timeout: MagicMock(status_code=200, content=_png_bytes()),
    )
    images = reference_images.get_reference_images_as_bytes("joelho")
    assert images and images[0][1] == "image/png"


def test_generic_folder_not_used_for_specific_region(tmp_path, monkeypatch):
    (tmp_path / "geral").mkdir()
    (tmp_path / "geral" / "qualquer.png").write_bytes(_png_bytes())
    monkeypatch.setattr(reference_images, "REFERENCE_DATA_DIR", tmp_path)
    monkeypatch.setattr(reference_images, "FALLBACK_URLS", {})

    assert reference_images.get_reference_images_as_bytes("cotovelo") == []
    assert len(reference_images.get_reference_images_as_bytes("geral")) == 1


# ── Cota do trial por IP (servidor) ───────────────────────────────────────────

def _trial_post(client, ip="198.51.100.20"):
    return client.post(
        "/trial/analyze",
        data={
            "exam_image": (io.BytesIO(_png_bytes()), "joelho.png"),
            "description": "dor no joelho",
            "privacy_consent": "accepted",
        },
        content_type="multipart/form-data",
        environ_base={"REMOTE_ADDR": ip},
    )


def test_trial_quota_is_enforced_server_side(client, monkeypatch):
    monkeypatch.setenv("TRIAL_MAX_PER_IP_PER_DAY", "2")
    monkeypatch.setenv("TRIAL_RATE_LIMIT", "100 per hour")
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", MagicMock(return_value={
        "exam_type": "joelho", "analysis": "laudo", "references_used": 0,
        "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [],
    }))

    first, second, third = (_trial_post(client) for _ in range(3))

    assert first.get_json()["trial_remaining"] == 1
    assert second.get_json()["trial_remaining"] == 0
    assert third.status_code == 429
    assert third.get_json()["trial_exhausted"] is True
    # Outro IP tem a própria cota.
    assert _trial_post(client, ip="198.51.100.21").status_code == 200


def test_trial_quota_does_not_store_raw_ip(client, monkeypatch):
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", MagicMock(return_value={
        "exam_type": "joelho", "analysis": "laudo", "references_used": 0,
        "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [],
    }))
    _trial_post(client, ip="198.51.100.77")
    with cost_tracking._memory_lock:
        scopes = list(cost_tracking._memory_spend)
    assert not any("198.51.100.77" in scope for scope in scopes)


# ── Armazenamento dos contadores ──────────────────────────────────────────────

def test_counters_use_supabase_when_no_redis(monkeypatch):
    """Sem Redis, os contadores não podem ficar só na memória de cada instância serverless."""
    monkeypatch.setattr(cost_tracking, "_get_redis_client", lambda: None)
    somar = MagicMock(return_value=7.5)
    buscar = MagicMock(return_value=7.5)
    monkeypatch.setattr(cost_tracking.db, "somar_gasto_diario", somar)
    monkeypatch.setattr(cost_tracking.db, "buscar_gasto_diario", buscar)

    assert cost_tracking.add_spend("trial", 2.5) == 7.5
    assert cost_tracking.get_spend("trial") == 7.5
    somar.assert_called_once()
    assert somar.call_args.args[0] == "trial" and somar.call_args.args[2] == 2.5


def test_redis_uri_also_read_from_queue_variable(monkeypatch):
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("ANALYSIS_QUEUE_REDIS_URL", "rediss://fila.example:6379")
    assert cost_tracking._redis_uri() == "rediss://fila.example:6379"


# ── Modelos configuráveis ─────────────────────────────────────────────────────

def test_pretty_model_name():
    assert analyzer.pretty_model_name("claude-sonnet-4-6") == "Claude Sonnet 4.6"
    assert analyzer.pretty_model_name("gemini-2.5-flash-lite") == "Gemini 2.5 Flash Lite"


def test_consensus_labels_come_from_models_actually_used():
    with flask_app_module.app.test_request_context("/"):
        info = presentation.build_consensus_info("gemini-2.5-pro + claude-opus-5-5 → consenso", "")
    assert info["model_a"] == "Gemini 2.5 Pro"
    assert info["model_b"] == "Claude Opus 5.5"


def test_claude_model_is_configurable(monkeypatch):
    monkeypatch.setattr(analyzer, "_CLAUDE_MODEL", "claude-sonnet-5-5")
    client = MagicMock()
    client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="laudo")], usage=MagicMock(input_tokens=10, output_tokens=5)
    )
    monkeypatch.setattr(analyzer.anthropic, "Anthropic", lambda **kwargs: client)

    text, usage = analyzer._analyze_with_claude("k", [(b"x", "image/png")], "joelho", "", [])

    assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-5-5"
    assert usage["model"] == "claude-sonnet-5-5"


def test_default_budget_leaves_room_for_synthesis():
    """Com o default antigo (22s), a reserva de 12s da síntese quase nunca cabia."""
    assert analyzer._ANALYSIS_BUDGET_SECONDS > analyzer._SYNTHESIS_RESERVE_SECONDS * 3


# ── Redimensionamento de imagem ───────────────────────────────────────────────

def _image_bytes(size, fmt="JPEG", mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, size, 128 if mode != "RGB" else (128, 128, 128)).save(buffer, format=fmt)
    return buffer.getvalue()


def test_small_supported_image_is_untouched():
    original = _image_bytes((800, 600))
    assert analyzer._process_image_bytes(original) == (original, "image/jpeg")


def test_large_image_is_downscaled():
    out, mime = analyzer._process_image_bytes(_image_bytes((4032, 3024)))
    assert max(Image.open(io.BytesIO(out)).size) == analyzer._MAX_IMAGE_EDGE_PX
    assert mime == "image/jpeg"


def test_png_stays_png_after_resize():
    out, mime = analyzer._process_image_bytes(_image_bytes((3000, 2000), fmt="PNG", mode="L"))
    assert mime == "image/png"
    assert Image.open(io.BytesIO(out)).mode == "L"


def test_16_bit_image_keeps_contrast():
    img = Image.new("I;16", (2000, 1000), 1000)
    img.paste(30000, (0, 0, 1000, 1000))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")

    out, _ = analyzer._process_image_bytes(buffer.getvalue())

    result = Image.open(io.BytesIO(out))
    assert result.getextrema() == (0, 255)  # antes: convert("L") cortava tudo em 255


def test_exif_orientation_is_applied():
    img = Image.new("RGB", (2000, 1000))
    exif = img.getexif()
    exif[0x0112] = 6  # girada 90°
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif)

    out, _ = analyzer._process_image_bytes(buffer.getvalue())

    width, height = Image.open(io.BytesIO(out)).size
    assert height > width


# ── Chamadas abandonadas ──────────────────────────────────────────────────────

def test_cancel_event_stops_new_gemini_attempts():
    client = MagicMock()
    cancel = threading.Event()
    cancel.set()
    try:
        analyzer._generate_with_retry(client, "gemini-2.5-flash", ["x"], cancel_event=cancel)
    except TimeoutError:
        pass
    client.models.generate_content.assert_not_called()


def test_late_usage_is_reported_when_abandoned_call_finishes():
    reported = []
    future = concurrent.futures.Future()
    future.set_running_or_notify_cancel()  # já em execução: não dá para cancelar

    analyzer._report_late_usage(future, reported.append, lambda result: result["usage"])
    future.set_result({"usage": {"model": "claude-sonnet-4-6", "input_tokens": 100, "output_tokens": 50}})

    assert reported == [{"model": "claude-sonnet-4-6", "input_tokens": 100, "output_tokens": 50}]


def test_late_usage_not_reported_for_call_that_never_started():
    reported = []
    analyzer._report_late_usage(concurrent.futures.Future(), reported.append, lambda r: r)
    assert reported == []


def test_late_usage_recorder_credits_budget_scopes():
    record = cost_tracking.late_usage_recorder([("global", 10.0), ("trial", 3.0)])
    record({"model": "gemini-2.5-flash", "input_tokens": 1_000_000, "output_tokens": 0})

    expected = cost_tracking.estimate_cost_usd("gemini-2.5-flash", 1_000_000, 0)
    assert cost_tracking.get_spend("global") == expected
    assert cost_tracking.get_spend("trial") == expected
