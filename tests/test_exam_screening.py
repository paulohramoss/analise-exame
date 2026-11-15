"""Triagem antes do laudo de imagem: fotos que não são exame recebem mensagem clara."""

import json
from unittest.mock import MagicMock

import pytest

from core import analyzer
from core.analyzer import ExamReadError


def _response(text: str, input_tokens: int = 100, output_tokens: int = 20) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.usage_metadata = MagicMock(prompt_token_count=input_tokens, candidates_token_count=output_tokens)
    return response


def _screening(*items: tuple[str, str]) -> str:
    return json.dumps({"imagens": [
        {"numero": i, "tipo": tipo, "descricao": descricao} for i, (tipo, descricao) in enumerate(items, 1)
    ]})


@pytest.fixture
def screening_on(monkeypatch):
    monkeypatch.setattr(analyzer, "_EXAM_SCREENING_ENABLED", True)
    monkeypatch.setattr(analyzer, "get_reference_images_as_bytes", lambda exam_type: [])
    monkeypatch.setattr(analyzer, "_make_gemini_client", lambda api_key, *args: MagicMock())
    monkeypatch.setattr(analyzer, "_process_image_bytes", lambda raw: (raw, "image/jpeg"))


def _analyze(images: list[bytes], **kwargs):
    return analyzer.analyze_exam_from_bytes(
        [(data, "image/jpeg") for data in images], "joelho.jpg", api_key="fake-key",
        user_description=kwargs.pop("user_description", "dor no joelho"), **kwargs,
    )


def test_non_exam_photo_is_rejected_with_user_message(screening_on, monkeypatch):
    generate = MagicMock(return_value=_response(_screening(("outro", "Foto de um cachorro."))))
    monkeypatch.setattr(analyzer, "_generate_with_retry", generate)

    with pytest.raises(ExamReadError) as exc_info:
        _analyze([b"foto-do-cachorro"])

    message = str(exc_info.value)
    assert "não parece ser um exame médico" in message
    assert "foto de um cachorro" in message
    assert generate.call_count == 1  # não gastou a análise completa


def test_rejected_screening_cost_is_still_recorded(screening_on, monkeypatch):
    monkeypatch.setattr(analyzer, "_generate_with_retry", MagicMock(return_value=_response(_screening(("outro", "gato")))))
    recorded = []

    with pytest.raises(ExamReadError):
        _analyze([b"foto-do-gato"], on_late_usage=recorded.append)

    assert recorded == [{"model": "gemini-2.5-flash", "input_tokens": 100, "output_tokens": 20}]


def test_exam_image_passes_and_screening_cost_joins_the_result(screening_on, monkeypatch):
    generate = MagicMock(side_effect=[
        _response(_screening(("exame", "ressonância do joelho"))),
        _response("## 1. IDENTIFICAÇÃO DO EXAME\nlaudo", 1000, 500),
    ])
    monkeypatch.setattr(analyzer, "_generate_with_retry", generate)
    stages = []

    result = _analyze([b"rm-joelho"], on_progress=stages.append)

    assert result["success"] is True
    assert stages[0] == "verificando_imagens"
    assert result["usage"] == [
        {"model": "gemini-2.5-flash", "input_tokens": 100, "output_tokens": 20},
        {"model": "gemini-2.5-flash", "input_tokens": 1000, "output_tokens": 500},
    ]


def test_medical_document_is_not_rejected(screening_on, monkeypatch):
    generate = MagicMock(side_effect=[
        _response(_screening(("documento", "laudo de ressonância"))),
        _response("laudo"),
    ])
    monkeypatch.setattr(analyzer, "_generate_with_retry", generate)

    assert _analyze([b"laudo-impresso"])["success"] is True


def test_screening_failure_does_not_block_the_analysis(screening_on, monkeypatch):
    generate = MagicMock(side_effect=[TimeoutError("lento"), _response("laudo")])
    monkeypatch.setattr(analyzer, "_generate_with_retry", generate)

    assert _analyze([b"rm-joelho-2"])["success"] is True


def test_unreadable_screening_answer_does_not_block_the_analysis(screening_on, monkeypatch):
    generate = MagicMock(side_effect=[_response("não sei"), _response("laudo")])
    monkeypatch.setattr(analyzer, "_generate_with_retry", generate)

    assert _analyze([b"rm-joelho-3"])["success"] is True


def test_structured_exams_skip_the_image_screening(screening_on, monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(analyzer, "screen_exam_images", called)
    monkeypatch.setattr(analyzer, "_analyze_structured_exam", lambda *a, **k: {"success": True})

    _analyze([b"hemograma"], exam_category="laboratorial")

    called.assert_not_called()


def test_rejected_images_ignores_out_of_range_and_non_rejections():
    text = json.dumps({"imagens": [
        {"numero": 1, "tipo": "exame", "descricao": "raio-x"},
        {"numero": 2, "tipo": "OUTRO", "descricao": "Selfie."},
        {"numero": 9, "tipo": "outro", "descricao": "fora do intervalo"},
    ]})
    assert analyzer._rejected_images(text, 3) == [(2, "selfie")]
    assert analyzer._rejected_images("```json\n" + text + "\n```", 3) == [(2, "selfie")]
    assert analyzer._rejected_images("resposta sem json", 3) == []


def test_messages_for_multiple_images():
    one = analyzer._screening_message([(2, "gato")], 3)
    assert one.startswith("A imagem 2 (gato) não parece ser exame médico")

    several = analyzer._screening_message([(1, "gato"), (3, "")], 3)
    assert several.startswith("As imagens 1 (gato) e 3 não parecem ser exame médico")

    all_of_them = analyzer._screening_message([(1, "gato"), (2, "carro")], 2)
    assert all_of_them.startswith("Nenhuma das imagens enviadas parece ser um exame médico")
