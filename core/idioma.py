"""
Idioma do laudo: português do Brasil (padrão) ou inglês.

A análise é sempre feita em português — prompts, consenso das duas IAs,
marcação dos achados, referências do PubMed e as regras dos exames
laboratoriais e de dinamometria dependem do texto em português. Quando o
profissional escolhe inglês, uma etapa final traduz o resultado pronto:

  - o laudo inteiro (Markdown), mantendo títulos numerados, tabelas, números e
    as etiquetas de evidência, que viram [STRONG] / [MODERATE] / [WEAK] / [TO CONFIRM];
  - os nomes e locais dos achados marcados nas imagens;
  - os textos dos padrões do painel laboratorial.

Se a tradução falhar, o laudo fica em português e o resultado diz isso
(`idioma` volta a "pt-BR" e `traducao_falhou` = True): nunca se perde o laudo.

Textos que a IA escreve do zero (parecer de correlação, explicação para o
paciente) não passam por aqui: o prompt deles já pede o idioma escolhido
(`instrucao_escrita`).
"""

import json
import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)

PADRAO = "pt-BR"
IDIOMAS = {"pt-BR": "Português (Brasil)", "en": "English"}

# Etiquetas de força de evidência: o laudo em inglês usa as equivalentes.
ETIQUETAS_EN = {
    "[FORTE]": "[STRONG]", "[MODERADO]": "[MODERATE]", "[FRACO]": "[WEAK]", "[A CONFIRMAR]": "[TO CONFIRM]",
}

REGIOES_EN = {
    "joelho": "Knee", "coluna": "Spine", "ombro": "Shoulder", "quadril": "Hip",
    "pe_tornozelo": "Foot and ankle", "mao_punho": "Hand and wrist", "cotovelo": "Elbow",
    "geral": "General", "dinamometria": "Dynamometry", "laboratorial": "Laboratory tests",
    "correlacao": "Exam correlation",
}


def normalizar(valor: str | None) -> str:
    """'en', 'EN', 'en-US', 'english' → 'en'; qualquer outra coisa → 'pt-BR'."""
    texto = (valor or "").strip().lower()
    return "en" if texto in {"en", "en-us", "en-gb", "english", "inglês", "ingles"} else PADRAO


def em_ingles(idioma: str | None) -> bool:
    return normalizar(idioma) == "en"


def contar_etiquetas(texto: str) -> tuple[int, int]:
    """(achados fortes, achados moderados) do laudo, em qualquer dos dois idiomas."""
    texto = texto or ""
    return (texto.count("[FORTE]") + texto.count("[STRONG]"),
            texto.count("[MODERADO]") + texto.count("[MODERATE]"))


def instrucao_escrita(idioma: str | None) -> str:
    """Instrução de idioma para prompts que geram texto do zero ('' em português)."""
    if not em_ingles(idioma):
        return ""
    return (
        "\n\nIDIOMA DE SAÍDA — OBRIGATÓRIO: escreva TODO o texto em inglês clínico (en-US), "
        "inclusive os títulos das seções (mantenha a numeração \"## 1.\", \"## 2.\"…). "
        "Use a terminologia anatômica e radiológica padrão em inglês, ponto como separador decimal "
        "e datas no formato AAAA-MM-DD. As instruções acima estão em português, mas a resposta deve "
        "estar inteiramente em inglês."
    )


# ── Tradução do resultado ─────────────────────────────────────────────────────

_PROMPT_LAUDO = """You are a medical translator. Translate the clinical report below from Brazilian
Portuguese into clinical English (en-US), for a physician or physical therapist outside Brazil.

Rules:
- Translate everything, including section headings. Keep the Markdown structure exactly: the same
  headings with the same numbers ("## 3." stays "## 3."), lists, tables with the same rows and columns,
  bold, italics and blockquotes.
- Evidence tags must become exactly: [FORTE] → [STRONG], [MODERADO] → [MODERATE], [FRACO] → [WEAK],
  [A CONFIRMAR] → [TO CONFIRM].
- Keep every number, unit, reference range, laterality and image number ("imagem 3" → "image 3").
  Write decimals with a point and thousands with a comma (14,6 → 14.6; 8.610 → 8,610).
  Write dates as YYYY-MM-DD.
- Use standard English anatomical, radiological, laboratory and rehabilitation terms
  (e.g. "ligamento cruzado anterior" → "anterior cruciate ligament", "TGO" → "AST", "TGP" → "ALT").
- Do not add, remove, merge or summarize anything. Do not add notes or comments.
- Keep article titles, authors, journals, PMIDs and links exactly as they are.
- Answer with the translated report only.

REPORT:
"""

_PROMPT_ITENS = """You are a medical translator. Translate every string value in the JSON below from
Brazilian Portuguese into clinical English (en-US). Keep the same keys, the same order and the same number
of items. Keep numbers, units and exam names that are proper names; use a point as decimal separator.
Answer with the JSON only.

JSON:
"""


def _itens_para_traduzir(result: dict) -> dict:
    itens: dict = {}
    marcacoes = result.get("annotations") or []
    if marcacoes:
        itens["marcacoes"] = [{"estrutura": m.get("estrutura", ""), "local": m.get("local", "")} for m in marcacoes]
    padroes = (result.get("structured") or {}).get("padroes") or []
    if padroes:
        itens["padroes"] = [{k: p.get(k, "") for k in ("titulo", "explicacao", "sugestao")} for p in padroes]
    return itens


def _parse_json(texto: str) -> dict | None:
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", texto)
    try:
        dados = json.loads(texto)
    except (TypeError, ValueError):
        inicio, fim = texto.find("{"), texto.rfind("}")
        if inicio < 0 or fim <= inicio:
            return None
        try:
            dados = json.loads(texto[inicio:fim + 1])
        except ValueError:
            return None
    return dados if isinstance(dados, dict) else None


def _titulos(texto: str) -> list[str]:
    return re.findall(r"(?m)^\s*#{1,3}\s*(\d+)\.", texto or "")


def laudo_traduzido_valido(original: str, traduzido: str) -> bool:
    """A tradução manteve o laudo? Mesmas seções numeradas, nada cortado, etiquetas convertidas."""
    traduzido = (traduzido or "").strip()
    if not traduzido or len(traduzido) < 0.5 * len(original.strip()):
        return False
    if _titulos(original) != _titulos(traduzido):
        return False
    return not any(tag in traduzido for tag in ETIQUETAS_EN)


def _aplicar_etiquetas(texto: str) -> str:
    for pt, en in ETIQUETAS_EN.items():
        texto = texto.replace(pt, en)
    return texto


def traduzir_resultado(result: dict, gerar: Callable[[str, bool], tuple[str, dict | None]]) -> dict:
    """Traduz o resultado de uma análise para inglês, no próprio dicionário.

    gerar(prompt, json_mode) → (texto, uso de tokens) chama a IA. O uso vai para
    result["usage"], para entrar no custo da análise.
    """
    original = result.get("analysis") or ""
    usage = result.setdefault("usage", [])

    try:
        texto, uso = gerar(_PROMPT_LAUDO + original, False)
        if uso:
            usage.append(uso)
        texto = _aplicar_etiquetas((texto or "").strip())
        if not laudo_traduzido_valido(original, texto):
            raise ValueError("tradução incompleta ou com seções diferentes")
    except Exception:
        logger.warning("Tradução do laudo falhou; o laudo segue em português.", exc_info=True)
        result.update(idioma=PADRAO, traducao_falhou=True)
        return result

    result.update(analysis=texto, idioma="en", traducao_falhou=False)
    if isinstance(result.get("structured"), dict):
        result["structured"]["idioma"] = "en"  # o painel salvo sabe em que idioma estão os padrões

    itens = _itens_para_traduzir(result)
    if not itens:
        return result
    try:
        resposta, uso = gerar(_PROMPT_ITENS + json.dumps(itens, ensure_ascii=False), True)
        if uso:
            usage.append(uso)
        traduzidos = _parse_json(resposta) or {}
    except Exception:
        logger.warning("Tradução das marcações/padrões falhou; ficam em português.", exc_info=True)
        return result

    marcacoes = traduzidos.get("marcacoes")
    if isinstance(marcacoes, list) and len(marcacoes) == len(result.get("annotations") or []):
        for alvo, novo in zip(result["annotations"], marcacoes):
            if isinstance(novo, dict):
                for campo in ("estrutura", "local"):
                    if isinstance(novo.get(campo), str) and novo[campo].strip():
                        alvo[campo] = novo[campo].strip()[:160]
    padroes = traduzidos.get("padroes")
    origem = (result.get("structured") or {}).get("padroes") or []
    if isinstance(padroes, list) and len(padroes) == len(origem):
        for alvo, novo in zip(origem, padroes):
            if isinstance(novo, dict):
                for campo in ("titulo", "explicacao", "sugestao"):
                    if isinstance(novo.get(campo), str) and novo[campo].strip():
                        alvo[campo] = novo[campo].strip()[:600]
    return result
