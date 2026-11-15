"""A resposta inteira cabe no orçamento (90 s): laudo, marcações e referências."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from core import analyzer
from core.env import configured, is_placeholder


def test_env_example_values_count_as_not_configured():
    assert is_placeholder("sua_chave_gemini_aqui")
    assert is_placeholder("https://SEU-PROJETO.supabase.co")
    assert configured("  sua_service_role_key ") == ""
    assert configured("AIzaSyRealKey") == "AIzaSyRealKey"
    assert configured("https://tjbiitmextikphjhzfcf.supabase.co").endswith(".supabase.co")


def test_partial_laudo_cuts_at_last_paragraph_and_warns():
    body = "## 1. ACHADOS\n" + "texto do laudo. " * 120 + "\n\n## 2. CONCLUSÃO\nconclusão pela met"
    partial = analyzer._partial_laudo(body)
    assert "conclusão pela met" not in partial
    assert partial.endswith(analyzer._DEADLINE_NOTICE)
    assert analyzer._partial_laudo("curto demais") is None


class _FakeStream:
    """Stream do Claude que nunca termina a tempo: o relógio avança a cada trecho."""

    def __init__(self, clock, chunks):
        self._clock, self._chunks = clock, chunks
        self.current_message_snapshot = SimpleNamespace(usage=SimpleNamespace(input_tokens=900, output_tokens=1))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for chunk in self._chunks:
            self._clock[0] += 10
            yield chunk


def _claude_only(monkeypatch, chunks, keep_partial):
    clock = [0.0]
    monkeypatch.setattr(analyzer.time, "monotonic", lambda: clock[0])
    client = MagicMock()
    client.messages.stream.return_value = _FakeStream(clock, chunks)
    monkeypatch.setattr(analyzer, "_make_anthropic_client", lambda *a, **k: client)
    monkeypatch.setattr(analyzer, "_build_claude_content", lambda *a, **k: [])
    return analyzer._analyze_with_claude(
        "key", [], "joelho", "", [], stop_at=lambda: 35.0, keep_partial=keep_partial,
    )


def test_claude_only_returns_partial_laudo_at_deadline(monkeypatch):
    paragraph = "achado relevante descrito em detalhe. " * 20 + "\n\n"
    text, usage = _claude_only(monkeypatch, [paragraph] * 6, keep_partial=True)
    assert text.endswith(analyzer._DEADLINE_NOTICE)
    assert usage["input_tokens"] == 900
    assert usage["output_tokens"] > 1  # estimado pelo texto recebido


def test_dual_mode_still_drops_late_claude(monkeypatch):
    paragraph = "achado relevante descrito em detalhe. " * 20 + "\n\n"
    assert _claude_only(monkeypatch, [paragraph] * 6, keep_partial=False) is None


def test_annotations_are_skipped_without_time_and_partial_is_not_cached(monkeypatch):
    monkeypatch.setattr(analyzer, "_ANNOTATIONS_ENABLED", True)
    monkeypatch.setattr(analyzer, "get_reference_images_as_bytes", lambda exam_type: [])
    monkeypatch.setattr(analyzer, "_process_image_bytes", lambda raw: (raw, "image/jpeg"))
    monkeypatch.setattr(analyzer, "_format_validated_case_reference", lambda case: None)
    monkeypatch.setattr(analyzer.db, "buscar_caso_validado_referencia", lambda t: None)
    clock = [1000.0]
    monkeypatch.setattr(analyzer.time, "monotonic", lambda: clock[0])

    def _slow_laudo(*args, **kwargs):
        clock[0] += 84  # laudo terminou a 1 s do prazo
        return "laudo" + analyzer._DEADLINE_NOTICE, analyzer._CLAUDE_MODEL, []

    monkeypatch.setattr(analyzer, "_produce_final_analysis", _slow_laudo)
    locate = MagicMock()
    monkeypatch.setattr(analyzer, "_locate_findings", locate)
    store = MagicMock()
    monkeypatch.setattr(analyzer, "_store_cached_analysis", store)

    result = analyzer.analyze_exam_from_bytes(
        (b"rm", "image/jpeg"), "joelho.jpg", api_key="sua_chave_gemini_aqui", anthropic_api_key="sk-ant-real",
    )

    assert result["success"] is True
    locate.assert_not_called()
    store.assert_not_called()
