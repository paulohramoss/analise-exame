"""
Testes de regressão para a lógica de análise (core/analyzer.py):
- estrutura do prompt enviado ao Gemini/Claude
- retry/fallback de modelo do Gemini
- classificação de erros retentáveis vs. definitivos
- truncamento de texto para a síntese de consenso
"""

from unittest.mock import MagicMock

import pytest

from core import analyzer
from core.reference_images import detect_exam_type

REQUIRED_SECTIONS = [
    "## 1. IDENTIFICAÇÃO DO EXAME",
    "## 2. ANÁLISE ESTRUTURAL",
    "## 3. ACHADOS",
    "## 4. IMPRESSÃO DIAGNÓSTICA",
    "## 5. IMPACTO NA ESTRUTURA FÍSICA DO PACIENTE",
    "## 6. CONDUTA E PRÓXIMOS PASSOS",
]


@pytest.mark.parametrize("exam_type", ["joelho", "coluna", "ombro", "quadril", "geral", "tipo_inexistente"])
def test_build_analysis_prompt_contains_required_sections(exam_type):
    """O laudo final depende dessas seções existirem com os títulos exatos.

    A síntese de consenso (_synthesize_analyses) referencia estes mesmos
    títulos na estrutura obrigatória do relatório — se um dos dois lados
    for renomeado sem o outro, o laudo final perde a seção.
    """
    prompt = analyzer.build_analysis_prompt(exam_type)
    for section in REQUIRED_SECTIONS:
        assert section in prompt


def test_build_analysis_prompt_multi_image_note():
    single = analyzer.build_analysis_prompt("joelho", num_images=1)
    multi = analyzer.build_analysis_prompt("joelho", num_images=3)
    assert "3 imagens" in multi
    assert "3 imagens" not in single


def test_synthesis_prompt_references_same_sections():
    """A síntese de consenso deve exigir a mesma estrutura de 6 seções do prompt individual."""
    import inspect

    source = inspect.getsource(analyzer._synthesize_analyses)
    for section in REQUIRED_SECTIONS:
        assert section in source


def test_is_quota_exhausted_true_for_resource_exhausted():
    assert analyzer._is_quota_exhausted(Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric"))


def test_is_quota_exhausted_false_for_per_minute_rate_limit():
    # Rate limit por minuto é transitório (retryable), diferente de cota diária esgotada.
    assert not analyzer._is_quota_exhausted(Exception("429 rate limit exceeded, per_minute quota"))


@pytest.mark.parametrize("message", ["503 Service Unavailable", "model overloaded", "HTTP 500", "429 rate limit"])
def test_is_retryable_true_for_transient_errors(message):
    assert analyzer._is_retryable(Exception(message))


def test_is_retryable_false_for_quota_exhausted():
    assert not analyzer._is_retryable(Exception("RESOURCE_EXHAUSTED: quota exceeded"))


def test_is_retryable_false_for_invalid_api_key():
    assert not analyzer._is_retryable(Exception("400 API key not valid"))


def test_generate_with_retry_succeeds_after_transient_failure(monkeypatch):
    monkeypatch.setattr(analyzer.time, "sleep", lambda *_: None)

    fake_response = MagicMock()
    client = MagicMock()
    client.models.generate_content.side_effect = [
        Exception("503 Service Unavailable"),
        fake_response,
    ]

    result = analyzer._generate_with_retry(client, "gemini-2.5-flash", contents=["oi"], deadline=None)

    assert result is fake_response
    assert client.models.generate_content.call_count == 2


def test_generate_with_retry_raises_immediately_on_non_retryable_error():
    client = MagicMock()
    client.models.generate_content.side_effect = Exception("400 API key not valid")

    with pytest.raises(Exception, match="API key not valid"):
        analyzer._generate_with_retry(client, "gemini-2.5-flash", contents=["oi"], deadline=None)

    # Não deve tentar novamente nem cair para modelo de fallback.
    assert client.models.generate_content.call_count == 1


def test_generate_with_retry_falls_back_to_next_model(monkeypatch):
    monkeypatch.setattr(analyzer.time, "sleep", lambda *_: None)
    monkeypatch.setattr(analyzer, "_MAX_RETRIES", 2)

    fake_response = MagicMock()
    client = MagicMock()
    client.models.generate_content.side_effect = [
        Exception("503 Service Unavailable"),
        Exception("503 Service Unavailable"),
        fake_response,
    ]

    result = analyzer._generate_with_retry(client, "gemini-2.5-flash", contents=["oi"], deadline=None)

    assert result is fake_response
    assert client.models.generate_content.call_count == 3
    called_models = [call.kwargs["model"] for call in client.models.generate_content.call_args_list]
    assert called_models == ["gemini-2.5-flash", "gemini-2.5-flash", "gemini-2.0-flash"]


def test_truncate_for_synthesis_keeps_short_text_untouched():
    text = "achado curto"
    assert analyzer._truncate_for_synthesis(text, max_chars=6000) == text


def test_truncate_for_synthesis_truncates_long_text():
    text = "A" * 5000 + "B" * 5000
    truncated = analyzer._truncate_for_synthesis(text, max_chars=2000)
    assert len(truncated) < len(text)
    assert "abreviado" in truncated
    assert truncated.startswith("A")
    assert truncated.endswith("B" * 10)


@pytest.mark.parametrize(
    "filename,description,expected",
    [
        ("joelho_direito.jpg", "", "joelho"),
        ("exame.jpg", "dor lombar com hernia de disco", "coluna"),
        ("mri.jpg", "manguito rotador ombro", "ombro"),
        ("foto.jpg", "sem informação clínica relevante", "geral"),
    ],
)
def test_detect_exam_type_from_keywords(filename, description, expected):
    assert detect_exam_type(filename, description) == expected
