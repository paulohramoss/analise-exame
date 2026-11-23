"""
Testes de ponta a ponta no navegador (Playwright + Chromium).

Sobe o app Flask de verdade numa thread, com o Supabase em memória
(e2e/fake_supabase.py) e a chamada de IA substituída por um resultado fixo —
o resto (rotas, templates, JavaScript, cifra do prontuário, comparação, metas)
roda como em produção.

    pip install playwright && playwright install chromium
    pytest e2e

Com o Chromium em outro lugar: E2E_CHROMIUM=/caminho/do/chrome pytest e2e
Capturas de tela de cada fluxo vão para e2e/capturas/ (E2E_SCREENSHOTS=0 desliga).
"""

import os
import socket
import threading
from pathlib import Path

import pytest

os.environ.update({
    "FLASK_SECRET_KEY": "e2e-secret",
    "GEMINI_API_KEY": "e2e-gemini-key",
    "ADMIN_KEY": "e2e-admin-key",
    "SUPABASE_URL": "",
    "SUPABASE_SERVICE_KEY": "",
    "SMTP_HOST": "",
    "EMAIL_MX_CHECK": "0",
    "SESSION_CACHE_TTL_SECONDS": "0",
    "ACCESS_LOG_ASYNC": "0",
    "ANNOTATIONS_ENABLED": "0",
    "PUBMED_REFERENCES_ENABLED": "0",
    "EXAM_SCREENING_ENABLED": "0",
    "LOCAL_PREMIUM_BYPASS": "0",
    "RATELIMIT_ENABLED": "false",
    "POSTHOG_API_KEY": "",
    "ANALYSIS_QUEUE_REDIS_URL": "",
    "RATELIMIT_STORAGE_URI": "memory://",
})

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

playwright_sync = pytest.importorskip("playwright.sync_api")

from e2e.fake_supabase import FakeSupabase  # noqa: E402

EMAIL = "fisio@example.com"
CAPTURAS = Path(__file__).parent / "capturas"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeAI:
    """Resultado de exame laboratorial fixo; cada chamada usa o próximo valor de LDL/ferritina."""

    def __init__(self):
        self.valores = [(121, 18), (153, 62)]
        self.chamadas = 0

    def __call__(self, **kwargs):
        from core import lab_tests

        ldl, ferritina = self.valores[min(self.chamadas, len(self.valores) - 1)]
        self.chamadas += 1
        dados = lab_tests.compute({
            "laboratorio": "Lab E2E", "data_coleta": None, "paciente": {},
            "resultados": [
                {"exame": "LDL", "valor": ldl, "valor_texto": "", "unidade": "mg/dL", "referencia_min": None,
                 "referencia_max": 130, "referencia_texto": "< 130", "anteriores": [], "duvida": None},
                {"exame": "Ferritina", "valor": ferritina, "valor_texto": "", "unidade": "ng/mL", "referencia_min": 30,
                 "referencia_max": 300, "referencia_texto": "30 a 300", "anteriores": [],
                 "duvida": "último dígito borrado" if self.chamadas == 1 else None},
                {"exame": "Glicose", "valor": 88, "valor_texto": "", "unidade": "mg/dL", "referencia_min": 70,
                 "referencia_max": 99, "referencia_texto": "70 a 99", "anteriores": [], "duvida": None},
            ],
        })
        return {
            "success": True, "exam_type": "laboratorial",
            "analysis": lab_tests.compose_report(dados, "## 3. INTERPRETAÇÃO\n- LDL acima da meta.\n- Ferritina a acompanhar."),
            "annotations": [], "references": [], "structured": dados, "references_used": 0,
            "model_used": "e2e (leitura)", "num_images": 1, "usage": [], "cache_hit": False,
        }


@pytest.fixture(scope="session")
def servidor():
    from werkzeug.serving import make_server

    import app as flask_app_module
    from core import analyzer, db

    banco = FakeSupabase()
    banco.tables["clientes"] = [{"id": "cliente-e2e", "email": EMAIL, "nome": "Ana Fisio", "sessao_versao": 0}]
    ia = FakeAI()
    original_client, original_analyze = db._get_client, analyzer.analyze_exam_from_bytes
    db._get_client = lambda: banco
    analyzer.analyze_exam_from_bytes = ia

    port = _free_port()
    server = make_server("127.0.0.1", port, flask_app_module.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{port}", "banco": banco, "ia": ia}
    server.shutdown()
    db._get_client, analyzer.analyze_exam_from_bytes = original_client, original_analyze


@pytest.fixture(scope="session")
def navegador():
    with playwright_sync.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("E2E_CHROMIUM") or None)
        yield browser
        browser.close()


@pytest.fixture()
def pagina(servidor, navegador):
    from web import security

    context = navegador.new_context(viewport={"width": 1280, "height": 900}, locale="pt-BR")
    context.add_cookies([{
        "name": security.PREMIUM_COOKIE,
        "value": security.generate_premium_token("pay_e2e", EMAIL, 3600),
        "url": servidor["url"],
    }])
    page = context.new_page()
    erros = []
    page.on("pageerror", lambda exc: erros.append(str(exc)))
    page.base = servidor["url"]
    yield page
    context.close()
    assert not erros, f"Erros de JavaScript na página: {erros}"


@pytest.fixture()
def capturar():
    def _capturar(page, nome: str) -> None:
        if os.environ.get("E2E_SCREENSHOTS", "1") == "0":
            return
        CAPTURAS.mkdir(exist_ok=True)
        page.screenshot(path=str(CAPTURAS / f"{nome}.png"), full_page=True)

    return _capturar
