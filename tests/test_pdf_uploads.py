"""
Exames em PDF: cada página vira imagem para a análise, o texto entra como
contexto clínico, e a identificação do paciente sai do texto e da imagem.
"""

import io
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageStat
from werkzeug.datastructures import FileStorage

from core import analyzer, privacy
from web import uploads

LINHAS_LAUDO = [
    "PACIENTE: MARIA DA SILVA",
    "CPF: 529.982.247-25    Prontuario: 778812",
    "Exame: RM do joelho direito",
    "Impressao: rotura do corno posterior do menisco medial.",
]


def pdf_com_texto(linhas: list[str]) -> bytes:
    """PDF mínimo de uma página com camada de texto (Helvetica), escrito à mão."""
    content = "BT /F1 12 Tf 50 780 Td 16 TL " + " ".join(f"({linha}) Tj T*" for linha in linhas) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def pdf_de_imagens(paginas: int = 1, branca: bool = False) -> bytes:
    """PDF só com imagem (como um filme exportado ou um laudo escaneado)."""
    def _pagina(i):
        img = Image.new("L", (400, 500), 255 if branca else 30)
        if not branca:
            img.paste(180 + i * 20, (100, 100, 300, 400))
        return img.convert("RGB")

    imgs = [_pagina(i) for i in range(paginas)]
    buffer = io.BytesIO()
    imgs[0].save(buffer, "PDF", save_all=True, append_images=imgs[1:])
    return buffer.getvalue()


def _upload(data: bytes, nome: str = "exame.pdf") -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=nome, content_type="application/pdf")


@pytest.fixture()
def limpar():
    criados = []
    yield criados
    for itens in criados:
        uploads.cleanup_upload_files(itens)


# ── Conversão ─────────────────────────────────────────────────────────────────

def test_pdf_e_aceito():
    assert uploads.allowed_file("laudo.PDF")


def test_laudo_em_pdf_vira_imagem_e_texto_sem_identificacao(limpar):
    [item] = itens = uploads.save_upload(_upload(pdf_com_texto(LINHAS_LAUDO)))
    limpar.append(itens)

    assert item["source"] == "pdf" and item["display_mime"] == "image/jpeg"
    assert item["original_name"] == "exame.pdf (pág. 1)" and item["pdf_has_text"]
    texto = item["pdf_text"]
    assert "rotura do corno posterior do menisco medial" in texto and "RM do joelho direito" in texto
    for identificador in ("MARIA", "SILVA", "529.982.247-25", "778812"):
        assert identificador not in texto
    assert "PACIENTE: [removido] CPF: [removido]" in texto  # o rótulo da linha de baixo continua


def test_identificacao_e_tarjada_na_imagem_da_pagina(limpar):
    [item] = itens = uploads.save_upload(_upload(pdf_com_texto(LINHAS_LAUDO)))
    limpar.append(itens)
    assert item["pdf_redactions"] > 0

    pdfium = pytest.importorskip("pypdfium2")
    page = pdfium.PdfDocument(pdf_com_texto(LINHAS_LAUDO))[0]
    textpage = page.get_textpage()
    text = textpage.get_text_range()
    image = Image.open(item["filepath"]).convert("L")
    scale = image.width / page.get_size()[0]
    nome = text.index("MARIA")
    left, bottom, right, top = textpage.get_charbox(nome)
    height = page.get_size()[1]
    regiao = image.crop((int(left * scale), int((height - top) * scale), int(right * scale), int((height - bottom) * scale)))
    assert ImageStat.Stat(regiao).mean[0] < 10  # a letra "M" do nome está coberta de preto

    exame = text.index("joelho")
    left, bottom, right, top = textpage.get_charbox(exame)
    regiao = image.crop((int(left * scale), int((height - top) * scale), int(right * scale), int((height - bottom) * scale)))
    assert ImageStat.Stat(regiao).mean[0] > 60  # o texto clínico continua legível


def test_pdf_so_com_imagem(limpar):
    itens = uploads.save_upload(_upload(pdf_de_imagens(3), "filme.pdf"))
    limpar.append(itens)
    assert [i["pdf_page"] for i in itens] == [1, 2, 3]
    assert all(not i["pdf_has_text"] and i["pdf_redactions"] == 0 for i in itens)
    assert "pdf_text" not in itens[0]
    assert len({str(i["original_path"]) for i in itens}) == 1  # as páginas compartilham o PDF original


def test_paginas_em_branco_sao_ignoradas(limpar):
    assert uploads.save_upload(_upload(pdf_de_imagens(1, branca=True))) == []


def test_limite_de_paginas_lidas(limpar, monkeypatch):
    monkeypatch.setattr(uploads, "PDF_MAX_PAGES", 2)
    itens = uploads.save_upload(_upload(pdf_de_imagens(4)))
    limpar.append(itens)
    assert [i["pdf_page"] for i in itens] == [1, 2] and itens[0]["pdf_pages"] == 4


def test_pdf_invalido_nao_deixa_arquivo(monkeypatch):
    antes = set(uploads.UPLOAD_FOLDER.iterdir())
    assert uploads.save_upload(_upload(b"%PDF-1.4 isto nao e um pdf")) == []
    assert set(uploads.UPLOAD_FOLDER.iterdir()) == antes


def test_contexto_inclui_o_texto_do_pdf(limpar):
    itens = uploads.save_upload(_upload(pdf_com_texto(LINHAS_LAUDO), "laudo-anterior.pdf"))
    limpar.append(itens)
    contexto = uploads.description_with_dicom_context("dor medial há 2 meses", itens)
    assert contexto.startswith("dor medial há 2 meses | Texto do PDF enviado (laudo-anterior.pdf)")
    assert "menisco medial" in contexto and "MARIA" not in contexto


# ── Identificadores (core/privacy.py) ─────────────────────────────────────────

@pytest.mark.parametrize("texto,removido", [
    ("PACIENTE: MARIA DA SILVA", "MARIA DA SILVA"),
    ("Nome do paciente: JOSÉ DOS SANTOS PEREIRA", "JOSÉ DOS SANTOS PEREIRA"),
    ("Prontuário: 123456", "123456"),
    ("Atendimento nº 98.765", "98.765"),
    ("Protocolo 2026/0001", "2026/0001"),
])
def test_identificadores_de_cabecalho_de_laudo(texto, removido):
    assert removido not in privacy.redact_identifiers(texto)[0]


def test_nome_nao_atravessa_a_linha():
    assert privacy.redact_identifiers("PACIENTE: MARIA DA SILVA\r\nRM do joelho")[0] == "PACIENTE: [removido]\r\nRM do joelho"


def test_posicoes_dos_identificadores():
    texto = "Paciente: Ana Lima\nCPF 529.982.247-25"
    assert [(texto[a:b], tipo) for a, b, tipo in privacy.find_identifier_spans(texto)] == [
        ("Ana Lima", "nome"), ("529.982.247-25", "cpf"),
    ]


# ── Rotas ─────────────────────────────────────────────────────────────────────

FAKE_RESULT = {
    "success": True, "exam_type": "joelho", "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\nlaudo",
    "references_used": 0, "model_used": "gemini-2.5-flash", "num_images": 1, "usage": [], "cache_hit": False,
}


def _login_as_admin(client):
    from web import config

    client.get(f"/admin/entrar?key={config.get_admin_key()}")


def test_analise_premium_de_pdf(client, monkeypatch):
    analyze = MagicMock(return_value=dict(FAKE_RESULT, num_images=2))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    _login_as_admin(client)

    resp = client.post("/analyze", data={
        "exam_images": [(io.BytesIO(pdf_com_texto(LINHAS_LAUDO)), "laudo.pdf"),
                        (io.BytesIO(pdf_de_imagens(1)), "filme.pdf")],
        "privacy_consent": "accepted",
        "description": "dor medial",
    }, content_type="multipart/form-data")

    assert resp.status_code == 200
    kwargs = analyze.call_args.kwargs
    assert len(kwargs["exam_images_data"]) == 2
    assert all(mime == "image/jpeg" for _, mime in kwargs["exam_images_data"])
    assert "menisco medial" in kwargs["user_description"] and "MARIA" not in kwargs["user_description"]


def test_paginas_alem_do_limite_ficam_de_fora(client, monkeypatch):
    analyze = MagicMock(return_value=dict(FAKE_RESULT))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)
    monkeypatch.setattr(uploads, "MAX_IMAGES_PER_ANALYSIS_ADMIN", 2)
    _login_as_admin(client)

    client.post("/analyze", data={
        "exam_images": [(io.BytesIO(pdf_de_imagens(4)), "serie.pdf")], "privacy_consent": "accepted",
    }, content_type="multipart/form-data")

    assert len(analyze.call_args.kwargs["exam_images_data"]) == 2


def test_teste_gratuito_analisa_a_primeira_pagina(client, monkeypatch):
    analyze = MagicMock(return_value=dict(FAKE_RESULT))
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", analyze)

    resp = client.post("/trial/analyze", data={
        "exam_image": (io.BytesIO(pdf_de_imagens(3)), "filme.pdf"), "privacy_consent": "accepted",
    }, content_type="multipart/form-data")

    assert resp.status_code == 200 and len(analyze.call_args.kwargs["exam_images_data"]) == 1


def test_pdf_protegido_ou_corrompido_avisa(client, monkeypatch):
    monkeypatch.setattr(analyzer, "analyze_exam_from_bytes", MagicMock(side_effect=AssertionError("não deveria analisar")))
    _login_as_admin(client)
    resp = client.post("/analyze", data={
        "exam_images": [(io.BytesIO(b"%PDF-1.4 corrompido"), "x.pdf")], "privacy_consent": "accepted",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "PDF protegido por senha não é aceito" in resp.get_data(as_text=True)
