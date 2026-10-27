"""
Relatório do laudo para o paciente: o laudo técnico reescrito em linguagem
simples, para o profissional imprimir (ou salvar em PDF) com a marca da
clínica e entregar ao paciente.

Privacidade: a IA recebe só o texto do laudo — que já não tem nome nem CPF
(core/privacy.py limpa a descrição antes da análise). O nome do paciente, quando
o laudo está ligado a um prontuário, entra apenas na página, nunca no prompt.

O texto não traz diagnóstico novo nem conduta: só explica o que o laudo diz e
orienta a conversar com o profissional responsável.
"""

import base64
import io
import logging
import os
import re

import anthropic
from markupsafe import Markup, escape
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

MODEL = os.environ.get("PATIENT_REPORT_MODEL", "").strip() or "claude-opus-5-5"
_MAX_TOKENS = 8000  # inclui o raciocínio (thinking adaptativo); o texto em si fica em ~800 palavras
_TIMEOUT_SECONDS = 90
_LAUDO_MAX_CHARS = 30_000

LOGO_MAX_BYTES = 2 * 1024 * 1024
_LOGO_MAX_SIDE = 400

SYSTEM_PROMPT = """Você escreve, em português do Brasil, a explicação de um laudo de exame de imagem \
musculoesquelético (ressonância, raio-X ou tomografia) para o próprio paciente ler em casa.

Quem lê não tem formação em saúde. Escreva como um profissional atencioso explicaria no consultório:
frases curtas, palavras do dia a dia, tom calmo e respeitoso. Quando um termo técnico for inevitável,
explique-o na mesma frase (por exemplo: "edema ósseo, que é um inchaço dentro do osso").

Use exatamente estas seções, com títulos em linhas começando por "## ":
## O que o exame mostrou
## O que isso significa
## Próximos passos

Regras:
- Explique apenas o que está no laudo. Não acrescente achados, diagnósticos, causas ou prognósticos.
- Não indique remédios, exercícios, cirurgias nem outro tratamento. Em "Próximos passos", oriente a \
conversar com o profissional que pediu o exame sobre o tratamento e anote dúvidas que valem a pena levar.
- Não minimize nem exagere: achados comuns ou leves podem ser descritos como tal se o laudo os descreve \
assim; achados importantes devem ser mencionados com clareza, sem alarmar.
- Ignore referências bibliográficas, nomes de modelos de IA, dados de responsável técnico e avisos legais do laudo.
- Use listas com "- " quando ajudar a leitura. Pode usar **negrito** para destacar poucas palavras.
- Não use tabelas, emojis nem outros títulos além dos três acima.
- Entre 250 e 600 palavras no total."""


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def gerar_texto(laudo: str) -> tuple[str, dict] | None:
    """(texto em linguagem simples, uso de tokens) ou None se a IA falhar ou recusar."""
    laudo = (laudo or "").strip()[:_LAUDO_MAX_CHARS]
    if not laudo or not is_configured():
        return None
    client = anthropic.Anthropic(timeout=_TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            # Reescrita de texto: esforço baixo basta e responde mais rápido.
            output_config={"effort": "low"},
            # Se o modelo recusar por engano (classificadores de segurança), o
            # próprio servidor refaz o pedido num modelo de reserva.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": f"Laudo do exame:\n\n{laudo}"}],
        )
    except anthropic.APIStatusError as e:
        logger.error("Relatório do paciente: erro da API (%s)", e.status_code)
        return None
    except anthropic.APIConnectionError:
        logger.error("Relatório do paciente: falha de conexão com a API")
        return None

    if response.stop_reason == "refusal":
        logger.warning("Relatório do paciente: pedido recusado pelo modelo")
        return None
    texto = "".join(block.text for block in response.content if block.type == "text").strip()
    if not texto:
        return None
    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return texto, usage


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", str(escape(text)))


def to_html(texto: str) -> Markup:
    """Markdown mínimo (## título, - lista, **negrito**, parágrafos) → HTML seguro.

    Tudo é escapado antes; só as tags geradas aqui passam. Não usa biblioteca de
    markdown para não aceitar HTML vindo do modelo.
    """
    html: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def close_paragraph():
        if paragraph:
            html.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list():
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    for raw in (texto or "").splitlines():
        line = raw.strip()
        if not line:
            close_paragraph()
            close_list()
        elif line.startswith("#"):
            close_paragraph()
            close_list()
            html.append(f"<h2>{_inline(line.lstrip('#').strip())}</h2>")
        elif line.startswith(("- ", "* ", "• ")):
            close_paragraph()
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_inline(line[2:].strip())}</li>")
        else:
            close_list()
            paragraph.append(line)
    close_paragraph()
    close_list()
    return Markup("\n".join(html))


def processar_logo(raw: bytes) -> str | None:
    """Logo enviado → data URL PNG reduzido, ou None se não for uma imagem válida.

    Reabrir e regravar a imagem descarta metadados e qualquer conteúdo que não
    seja pixel (SVG, scripts, EXIF com localização).
    """
    if not raw or len(raw) > LOGO_MAX_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((_LOGO_MAX_SIDE, _LOGO_MAX_SIDE))
            img = img.convert("RGBA")
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
    except Exception:
        return None
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
