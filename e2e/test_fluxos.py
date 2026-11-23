"""
Fluxos principais no navegador, em sequência, como o profissional usa:
cadastrar paciente → enviar exame → revisar o laudo → metas e lembretes →
segundo exame → "O que mudou" → tela Hoje.
"""

import io
import re
from datetime import date

from PIL import Image

from e2e.conftest import playwright_sync

expect = playwright_sync.expect


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (900, 1200), "white").save(buf, "PNG")
    return buf.getvalue()


def _enviar_exame(page, paciente_id: str) -> None:
    page.goto(f"{page.base}/?paciente={paciente_id}")
    page.select_option("#exam_category", "laboratorial")
    page.set_input_files("#exam_images", files=[{"name": "exame.png", "mimeType": "image/png", "buffer": _png()}])
    page.check("#privacy_consent")
    if page.locator("#termo_responsabilidade").count():
        page.check("#termo_responsabilidade")
    with page.expect_navigation():
        page.click("#submit-btn")


def test_fluxo_completo_do_profissional(pagina, servidor, capturar):
    page = pagina

    # 1. Tela Hoje vazia ensina o primeiro passo.
    page.goto(f"{page.base}/hoje")
    expect(page.get_by_text("Comece pelo primeiro paciente")).to_be_visible()
    capturar(page, "01-hoje-vazio")

    # 2. Cadastro do paciente.
    page.goto(f"{page.base}/pacientes/novo")
    page.fill("input[name=nome]", "Maria da Silva")
    page.fill("input[name=cpf]", "529.982.247-25")
    page.fill("input[name=data_nascimento]", "1990-05-01")
    page.check("input[name=consentimento]")
    with page.expect_navigation():
        page.locator("form button[type=submit]").last.click()
    expect(page.locator("h1.page-title")).to_have_text("Maria da Silva")
    paciente_id = re.search(r"/pacientes/([\w-]+)", page.url).group(1)
    linha = servidor["banco"].tables["pacientes"][0]
    assert "Maria" not in str(linha) and linha["nome_cifrado"]  # nome cifrado no banco

    # 3. Primeiro exame laboratorial, vinculado ao paciente.
    _enviar_exame(page, paciente_id)
    expect(page.get_by_text("Painel dos exames")).to_be_visible()
    expect(page.locator(".lab-row.lab-check")).to_have_count(1)  # ferritina com leitura incerta
    capturar(page, "02-resultado-laboratorial")

    # 4. Revisar e editar o laudo; a versão da IA fica guardada.
    with page.expect_navigation():
        page.get_by_role("link", name="Revisar e editar").click()
    editor = page.locator("#analise-edit")
    expect(editor).to_be_visible()
    editor.fill(editor.input_value() + "\n\n## 7. NOTA DO PROFISSIONAL\n- Repetir lipidograma em jejum.")
    with page.expect_navigation():
        page.get_by_role("button", name="Salvar minha versão").click()
    expect(page.get_by_text("Laudo revisado pelo profissional")).to_be_visible()
    expect(page.locator("#report-content")).to_contain_text("Repetir lipidograma")
    analise = servidor["banco"].tables["analises"][0]
    assert analise["analise_ia"] and "Repetir lipidograma" not in analise["analise_ia"]
    capturar(page, "03-laudo-editado")

    # 5. Meta e lembrete no prontuário.
    page.goto(f"{page.base}/pacientes/{paciente_id}")
    page.fill("#meta-item", "Ferritina")
    page.select_option("#meta-op", ">")
    page.fill("#meta-valor", "50")
    with page.expect_navigation():
        page.get_by_role("button", name="Adicionar meta").click()
    expect(page.locator("#metas")).to_contain_text("Ferritina > 50")
    expect(page.locator("#metas")).to_contain_text("Ainda não")
    page.fill("#lb-texto", "Repetir CK")
    page.fill("#lb-data", date.today().isoformat())
    with page.expect_navigation():
        page.get_by_role("button", name="Criar lembrete").click()
    expect(page.locator("#lembretes")).to_contain_text("Repetir CK")

    # 6. Segundo exame: a meta é atingida e "O que mudou" compara os dois.
    _enviar_exame(page, paciente_id)
    page.goto(f"{page.base}/pacientes/{paciente_id}")
    expect(page.locator("#metas")).to_contain_text("Atingida")
    with page.expect_navigation():
        page.get_by_role("link", name="O que mudou").click()
    ldl = page.locator("tr", has_text="LDL")
    expect(ldl).to_contain_text("121 mg/dL")
    expect(ldl).to_contain_text("153 mg/dL")
    expect(ldl).to_contain_text("Piorou")
    expect(page.locator("tr", has_text="Ferritina")).to_contain_text("Melhorou")
    page.check("#cmp-only-changed")
    expect(page.locator("tr.cmp-row", has_text="Glicose")).to_be_hidden()
    capturar(page, "04-o-que-mudou")

    # 7. Tela Hoje: lembrete do dia e meta que o último exame atingiu.
    page.goto(f"{page.base}/hoje")
    expect(page.get_by_text("Repetir CK")).to_be_visible()
    expect(page.get_by_text("atingiu a meta no último exame")).to_be_visible()
    capturar(page, "05-hoje")
    with page.expect_navigation():
        page.get_by_role("button", name="Feito").first.click()
    expect(page.get_by_text("Nenhuma reavaliação para hoje.")).to_be_visible()

    assert servidor["ia"].chamadas == 2


def test_painel_so_alterados_filtra(pagina, servidor):
    page = pagina
    analise = servidor["banco"].tables["analises"][-1]
    page.goto(f"{page.base}/laudos/{analise['id']}")
    expect(page.locator(".lab-row", has_text="Glicose")).to_be_visible()
    page.check(".lab-only-altered")
    expect(page.locator(".lab-row", has_text="Glicose")).to_be_hidden()
    expect(page.locator(".lab-row", has_text="LDL")).to_be_visible()
