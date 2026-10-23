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
  inteira, envolvendo de perto a estrutura alterada.
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
