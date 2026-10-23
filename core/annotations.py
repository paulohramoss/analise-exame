"""
Marcação dos achados do laudo dentro das imagens do exame.

Depois do laudo final, uma chamada própria pede ao modelo a localização de cada
achado em cada imagem: caixa normalizada (0-1000, no formato box_2d do Gemini:
[ymin, xmin, ymax, xmax]), a estrutura, o local anatômico exato e o grau de
certeza herdado do laudo:

    forte    → [FORTE] no laudo       → círculo verde
    moderado → [MODERADO] no laudo    → círculo amarelo
    baixo    → achados descartados    → círculo vermelho
              por evidência insuficiente

A marcação é aproximada (o modelo estima a região, não segmenta pixels) e é
sempre apresentada assim ao profissional. Este módulo só monta o pedido e
valida a resposta — a resposta vem de um modelo e pode vir malformada ou
influenciada pelo conteúdo do exame, então nada dela é usado sem checagem.
"""

import json
import os
import re

CERTEZAS = ("forte", "moderado", "baixo")
MAX_MARCACOES = int(os.environ.get("ANNOTATION_MAX_MARKS", "12"))
_MAX_TEXTO = 240

_ALIASES = {
    "forte": "forte", "alta": "forte", "alto": "forte",
    "moderado": "moderado", "moderada": "moderado", "media": "moderado", "média": "moderado",
    "baixo": "baixo", "baixa": "baixo", "fraco": "baixo", "fraca": "baixo",
}


def build_localization_prompt(analysis: str, num_images: int) -> str:
    """Pedido de localização dos achados já descritos no laudo final."""
    return f"""Você é um radiologista musculoesquelético. Abaixo está o laudo final já emitido para
{"a imagem" if num_images == 1 else f"as {num_images} imagens"} do exame que você recebeu acima.

TAREFA: localize, DENTRO DAS IMAGENS, cada achado do laudo, para que o profissional veja onde
está a estrutura alterada.

Regras:
- Use somente achados que estão no laudo. Não crie achados novos.
- "certeza":
  - "forte" para achados marcados [FORTE] no laudo;
  - "moderado" para achados marcados [MODERADO];
  - "baixo" para os achados listados como descartados por evidência insuficiente.
- "imagem": número da imagem do exame (1 a {num_images}) em que o achado é visível. Se o achado
  aparece em mais de uma imagem, repita o achado uma vez para cada imagem.
- "box_2d": [ymin, xmin, ymax, xmax] em coordenadas normalizadas de 0 a 1000 em relação à imagem
  inteira (0,0 = canto superior esquerdo; y cresce para baixo, x para a direita). A ordem é
  y antes de x. A caixa deve envolver JUSTO a alteração em si (o sinal anômalo, a rotura, o
  edema, o traço de fratura) — não a estrutura inteira nem a articulação toda.
- Use a imagem como ela aparece: não espelhe nem gire mentalmente, e ignore textos, réguas e
  bordas da tela do visualizador.
- Não marque nada em páginas de documento (laudo ou relatório em texto, ex.: página de PDF):
  só em imagens de exame.
- "estrutura": nome curto da estrutura (ex.: "Menisco medial").
- "local": localização anatômica exata (ex.: "corno posterior do menisco medial, junção
  meniscocapsular, joelho direito, plano sagital").
- Se um achado não é visível em nenhuma imagem (ex.: só deduzido do contexto), não o inclua.
- No máximo {MAX_MARCACOES} marcações. Se não houver achado localizável, devolva lista vazia.

Responda APENAS com JSON, sem texto antes ou depois, no formato:
{{"achados": [{{"imagem": 1, "estrutura": "...", "local": "...", "certeza": "forte", "box_2d": [0, 0, 0, 0]}}]}}

LAUDO FINAL:
{analysis}
"""


def build_refinement_prompt(estrutura: str, local: str) -> str:
    """Segunda passada: a IA vê só um recorte ampliado ao redor da primeira estimativa."""
    return f"""Esta imagem é um RECORTE AMPLIADO de um exame musculoesquelético, centrado onde foi
estimado o achado abaixo:

Estrutura: {estrutura}
Local: {local}

TAREFA: devolva a caixa que envolve JUSTO a alteração (o sinal anômalo, a rotura, o edema, o
traço de fratura) dentro DESTE recorte, em coordenadas normalizadas de 0 a 1000 relativas ao
recorte: [ymin, xmin, ymax, xmax] (y antes de x; 0,0 = canto superior esquerdo).
Se a alteração não estiver visível neste recorte, devolva "box_2d": null.

Responda APENAS com JSON: {{"box_2d": [ymin, xmin, ymax, xmax]}}
"""


def parse_refinement(text: str) -> list[int] | None:
    """Caixa do recorte (0-1000) ou None se ilegível/ausente."""
    data = _extract_json(text)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        return None
    return _clean_box(data.get("box_2d"))


def crop_window(box: list[int], context: float = 0.9, min_side: int = 160) -> list[int]:
    """Janela de recorte (0-1000) ao redor da caixa: a caixa + `context` de folga de cada
    lado, com pelo menos `min_side` (16% da imagem) para a IA enxergar o entorno."""
    ymin, xmin, ymax, xmax = box

    def _span(lo: int, hi: int) -> tuple[int, int]:
        half = max((hi - lo) * (1 + 2 * context), min_side) / 2
        center = (lo + hi) / 2
        start, end = center - half, center + half
        if start < 0:
            start, end = 0, min(1000, end - start)
        if end > 1000:
            start, end = max(0, start - (end - 1000)), 1000
        return int(round(start)), int(round(end))

    y0, y1 = _span(ymin, ymax)
    x0, x1 = _span(xmin, xmax)
    return [y0, x0, y1, x1]


def refined_box(window: list[int], local_box: list[int] | None, coarse: list[int]) -> list[int]:
    """Converte a caixa do recorte para a imagem inteira.

    Fica com a estimativa original quando o refinamento não ajuda: sem caixa, ou
    caixa que ocupa quase o recorte todo (a IA só devolveu o recorte de volta).
    """
    if not local_box:
        return coarse
    y0, x0, y1, x1 = window
    ly0, lx0, ly1, lx1 = local_box
    if (ly1 - ly0) * (lx1 - lx0) > 0.9 * 1000 * 1000:
        return coarse
    scale_y, scale_x = (y1 - y0) / 1000, (x1 - x0) / 1000
    box = [
        int(round(y0 + ly0 * scale_y)), int(round(x0 + lx0 * scale_x)),
        int(round(y0 + ly1 * scale_y)), int(round(x0 + lx1 * scale_x)),
    ]
    return _clean_box(box) or coarse


def _extract_json(text: str):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith(("{", "[")):
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            return None
        text = text[start:]
    try:
        return json.loads(text)
    except ValueError:
        return None


def _clean_text(value) -> str:
    return " ".join(str(value or "").split())[:_MAX_TEXTO]


def _clean_box(value) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (max(0, min(1000, int(round(float(v))))) for v in value)
    except (TypeError, ValueError):
        return None
    if ymax - ymin < 5 or xmax - xmin < 5:
        return None
    return [ymin, xmin, ymax, xmax]


def parse_localization(text: str, num_images: int) -> list[dict]:
    """Resposta do modelo → marcações válidas, numeradas e ordenadas por certeza.

    Itens sem caixa válida, sem estrutura, com certeza desconhecida ou com
    imagem fora do intervalo são descartados.
    """
    data = _extract_json(text)
    items = data.get("achados") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    marcacoes = []
    for item in items:
        if not isinstance(item, dict):
            continue
        certeza = _ALIASES.get(_clean_text(item.get("certeza")).lower())
        box = _clean_box(item.get("box_2d"))
        estrutura = _clean_text(item.get("estrutura"))
        try:
            imagem = int(item.get("imagem") or 1)
        except (TypeError, ValueError):
            continue
        if not certeza or not box or not estrutura or not 1 <= imagem <= num_images:
            continue
        marcacoes.append({
            "imagem": imagem,
            "estrutura": estrutura,
            "local": _clean_text(item.get("local")) or estrutura,
            "certeza": certeza,
            "box_2d": box,
        })

    marcacoes.sort(key=lambda m: (CERTEZAS.index(m["certeza"]), m["imagem"]))
    marcacoes = marcacoes[:MAX_MARCACOES]
    for numero, marcacao in enumerate(marcacoes, 1):
        marcacao["numero"] = numero
    return marcacoes


def sanitize_stored(value, num_images: int | None = None) -> list[dict]:
    """Marcações lidas do banco/cache: revalida antes de entregar ao template."""
    if not isinstance(value, list):
        return []
    limite = num_images or max((m.get("imagem", 1) for m in value if isinstance(m, dict)), default=1)
    return parse_localization(json.dumps({"achados": value}), int(limite or 1))
