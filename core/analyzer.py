"""
Módulo principal de análise de exames médicos.
Usa Gemini e Claude em paralelo para análise independente, depois sintetiza
um relatório de consenso que maximiza precisão diagnóstica.
"""

import base64
import concurrent.futures
import hashlib
import io
import logging
import os
import tempfile
import time
from pathlib import Path

import anthropic
from google import genai
from google.genai import types
from PIL import Image

logger = logging.getLogger(__name__)

# Modelos de fallback em ordem de preferência
_FALLBACK_MODELS = ["gemini-2.0-flash", "gemini-2.0-flash-lite"]

_RETRYABLE_CODES = {503, 429, 500}
_MAX_RETRIES = int(os.environ.get("AI_MAX_RETRIES", "2"))
_RETRY_DELAY = float(os.environ.get("AI_RETRY_DELAY_SECONDS", "1.0"))
_ANALYSIS_BUDGET_SECONDS = float(os.environ.get("ANALYSIS_BUDGET_SECONDS", "22"))
_GEMINI_HTTP_TIMEOUT_MS = int(os.environ.get("GEMINI_HTTP_TIMEOUT_MS", "22000"))
_GEMINI_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "3000"))
# thinking_budget=0 desabilita o thinking interno do Gemini 2.5 Flash, reduzindo
# latência em 10-30s sem impacto relevante na qualidade para prompts estruturados.
# Defina GEMINI_THINKING_BUDGET=-1 para restaurar o thinking dinâmico.
_GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))
_CLAUDE_ANALYSIS_TIMEOUT_SECONDS = float(os.environ.get("CLAUDE_ANALYSIS_TIMEOUT_SECONDS", "18"))
_CLAUDE_SYNTHESIS_TIMEOUT_SECONDS = float(os.environ.get("CLAUDE_SYNTHESIS_TIMEOUT_SECONDS", "8"))
_CLAUDE_ANALYSIS_MAX_TOKENS = int(os.environ.get("CLAUDE_ANALYSIS_MAX_TOKENS", "2500"))
_CLAUDE_SYNTHESIS_MAX_TOKENS = int(os.environ.get("CLAUDE_SYNTHESIS_MAX_TOKENS", "2000"))
_SYNTHESIS_INPUT_MAX_CHARS = int(os.environ.get("SYNTHESIS_INPUT_MAX_CHARS", "6000"))
_INCLUDE_REFERENCE_PDF = os.environ.get("INCLUDE_REFERENCE_PDF", "false").strip().lower() in {"1", "true", "yes", "on"}

# Cache do resultado completo de uma análise, por hash do conteúdo (imagens +
# descrição + modelo). Evita reprocessar a mesma imagem duas vezes — mesma
# limitação de escopo por processo que o _pdf_uri_cache abaixo.
_ANALYSIS_RESULT_CACHE_TTL_SECONDS = int(os.environ.get("ANALYSIS_RESULT_CACHE_TTL_SECONDS", str(24 * 3600)))
_ANALYSIS_RESULT_CACHE_ENABLED = os.environ.get("ANALYSIS_RESULT_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
_analysis_result_cache: dict = {}


def _remaining_seconds(deadline: float | None, fallback: float) -> float:
    if not deadline:
        return fallback
    return max(1.0, min(fallback, deadline - time.monotonic()))


def _deadline_expired(deadline: float | None, reserve_seconds: float = 0.0) -> bool:
    return bool(deadline and time.monotonic() + reserve_seconds >= deadline)


def _truncate_for_synthesis(text: str, max_chars: int = _SYNTHESIS_INPUT_MAX_CHARS) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2].rstrip()
    tail = text[-max_chars // 2 :].lstrip()
    return f"{head}\n\n[... conteúdo intermediário abreviado para acelerar a síntese ...]\n\n{tail}"


def _extract_gemini_usage(model: str, response: object) -> dict | None:
    usage_metadata = getattr(response, "usage_metadata", None)
    if not usage_metadata:
        return None
    return {
        "model": model,
        "input_tokens": usage_metadata.prompt_token_count or 0,
        "output_tokens": usage_metadata.candidates_token_count or 0,
    }


def _extract_claude_usage(model: str, message: object) -> dict | None:
    usage = getattr(message, "usage", None)
    if not usage:
        return None
    return {
        "model": model,
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
    }


def _analysis_cache_key(images_bytes: list[bytes], user_description: str, model_name: str, dual_ai: bool) -> str:
    hasher = hashlib.sha256()
    for data in images_bytes:
        hasher.update(data)
    hasher.update(user_description.strip().lower().encode("utf-8"))
    hasher.update(model_name.encode("utf-8"))
    hasher.update(b"dual" if dual_ai else b"single")
    return hasher.hexdigest()


def _get_cached_analysis(cache_key: str) -> dict | None:
    if not _ANALYSIS_RESULT_CACHE_ENABLED:
        return None
    entry = _analysis_result_cache.get(cache_key)
    if not entry:
        return None
    if entry["expires_at"] <= time.time():
        _analysis_result_cache.pop(cache_key, None)
        return None
    logger.info("Cache hit para análise (hash=%s...)", cache_key[:12])
    result = dict(entry["result"])
    result["cache_hit"] = True
    result["usage"] = []
    return result


def _store_cached_analysis(cache_key: str, result: dict) -> None:
    if not _ANALYSIS_RESULT_CACHE_ENABLED:
        return
    _analysis_result_cache[cache_key] = {
        "result": result,
        "expires_at": time.time() + _ANALYSIS_RESULT_CACHE_TTL_SECONDS,
    }


def _make_gemini_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=_GEMINI_HTTP_TIMEOUT_MS),
    )


def _is_quota_exhausted(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        ("resource_exhausted" in msg or "quota" in msg)
        and "rate" not in msg
        and "per_minute" not in msg
        and "per_second" not in msg
    )


def _is_retryable(exc: Exception) -> bool:
    if _is_quota_exhausted(exc):
        return False
    msg = str(exc).lower()
    return (
        "503" in msg
        or "unavailable" in msg
        or "429" in msg
        or "rate limit" in msg
        or "resource_exhausted" in msg
        or "overloaded" in msg
        or "500" in msg
    )


def _generate_with_retry(
    client: genai.Client,
    model_name: str,
    contents,
    max_output_tokens: int = _GEMINI_MAX_OUTPUT_TOKENS,
    deadline: float | None = None,
) -> object:
    """Chama generate_content com retry exponencial e fallback de modelos."""
    models_to_try = [model_name] + [m for m in _FALLBACK_MODELS if m != model_name]
    last_exc = None

    for model in models_to_try:
        delay = _RETRY_DELAY
        for attempt in range(1, _MAX_RETRIES + 1):
            if _deadline_expired(deadline, reserve_seconds=3):
                raise TimeoutError("Orçamento de tempo da análise esgotado antes da chamada ao Gemini.")
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        max_output_tokens=max_output_tokens,
                        temperature=0.1,
                        thinking_config=types.ThinkingConfig(thinking_budget=_GEMINI_THINKING_BUDGET),
                        http_options=types.HttpOptions(timeout=int(_remaining_seconds(deadline, _GEMINI_HTTP_TIMEOUT_MS / 1000) * 1000)),
                    ),
                )
                if model != model_name:
                    logger.info("Resposta obtida com modelo de fallback: %s", model)
                return response
            except Exception as exc:
                last_exc = exc
                if _is_retryable(exc):
                    if attempt < _MAX_RETRIES:
                        logger.warning(
                            "Tentativa %d/%d falhou (%s). Aguardando %.1fs...",
                            attempt, _MAX_RETRIES, exc, delay,
                        )
                        if _deadline_expired(deadline, reserve_seconds=delay):
                            break
                        time.sleep(delay)
                        delay *= 2
                    else:
                        logger.warning("Modelo %s esgotou %d tentativas. Tentando fallback...", model, _MAX_RETRIES)
                        break
                else:
                    raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Failed to generate content: all retry attempts exhausted and no exception was recorded.")

from core import db
from core.reference_images import (
    detect_exam_type,
    get_reference_images_as_bytes,
    get_reference_pdfs,
)


def _format_validated_case_reference(case: dict | None) -> str | None:
    """Formata um diagnóstico validado como bloco de exemplo de calibração para o prompt."""
    if not case:
        return None
    diagnostico_final = (case.get("diagnostico_final") or "").strip()
    if not diagnostico_final:
        return None

    grau = case.get("grau_concordancia")
    grau_label = f"{grau}%" if grau is not None else "não informada"
    lines = [
        f"**CASO CLÍNICO VALIDADO POR ESPECIALISTA (referência de calibração — mesmo tipo de exame, "
        f"concordância {grau_label}):**",
        diagnostico_final,
    ]
    achados_corretos = case.get("achados_corretos") or []
    if achados_corretos:
        lines.append("Achados confirmados neste caso de referência: " + "; ".join(achados_corretos))
    return "\n".join(lines)


# Cache in-memory para URIs de PDF do Gemini File API
_pdf_uri_cache: dict = {}


def _get_or_upload_pdf(client: genai.Client, pdf_bytes: bytes) -> tuple[str, str] | None:
    """Upload PDF para Gemini File API, cacheando o URI por 47h."""
    pdf_hash = hashlib.md5(pdf_bytes).hexdigest()

    cached = _pdf_uri_cache.get(pdf_hash)
    if cached and cached["expires_at"] > time.time():
        logger.debug("Reutilizando URI de PDF em cache: %s...", cached["uri"][:60])
        return cached["uri"], cached["mime"]

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        logger.info("Enviando %d KB para Gemini File API...", len(pdf_bytes) // 1024)
        file_response = client.files.upload(file=tmp_path)

        uri = file_response.uri
        mime = file_response.mime_type or "application/pdf"

        _pdf_uri_cache[pdf_hash] = {
            "uri": uri,
            "mime": mime,
            "expires_at": time.time() + 47 * 3600,
        }
        logger.info("Upload de PDF concluído. URI: %s...", uri[:60])
        return uri, mime

    except Exception:
        logger.warning("Upload de PDF falhou. Usando bytes inline como fallback.", exc_info=True)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def build_analysis_prompt(exam_type: str, num_images: int = 1) -> str:
    """Constrói o prompt especializado para análise ortopédica com filtro de evidência e impacto funcional."""
    region_map = {
        "joelho":       "Joelho (estruturas ósseas, meniscos, ligamentos, cartilagem)",
        "coluna":       "Coluna Vertebral (corpos vertebrais, discos, canal medular, forames)",
        "ombro":        "Ombro (glenoumeral, manguito rotador, acromioclavicular, bursa)",
        "quadril":      "Quadril (acetábulo, cabeça femoral, colo femoral, sacroilíaca)",
        "pe_tornozelo": "Pé e Tornozelo (calcâneo, tálus, metatarsos, ligamentos laterais/mediais)",
        "mao_punho":    "Mão e Punho (carpo, metacarpos, falanges, ligamentos intrínsecos)",
        "cotovelo":     "Cotovelo (úmero distal, rádio proximal, ulna, epicôndilos, olécrano)",
        "geral":        "Ortopédica (região a identificar pela imagem)",
    }
    region_label = region_map.get(exam_type, region_map["geral"])

    multi_image_note = ""
    if num_images > 1:
        multi_image_note = f"\n**NOTA:** Foram fornecidas **{num_images} imagens** do exame. Analise todas as imagens em conjunto, correlacionando os achados entre elas (diferentes planos, incidências ou sequências do mesmo exame).\n"

    return f"""
Você é um sistema de análise de imagens musculoesqueléticas da plataforma Three Health.
Seu objetivo é produzir a análise mais precisa, profunda e útil possível do exame fornecido.
Responda diretamente com o conteúdo estruturado abaixo — sem saudações, introduções, apresentações ou assinaturas.

**BASE DE CONHECIMENTO:**
- **Atlas(es) fornecido(s) acima** — referência primária de anatomia musculoesquelética normal
- **Gray's Anatomy** (41ª ed.) — anatomia topográfica do aparelho locomotor
- **Netter's Atlas of Human Anatomy** — referência visual de estruturas osteoarticulares
- **Helms – Fundamentals of Skeletal Radiology** — padrões radiológicos MSK normais e patológicos
- **Stoller – MRI in Orthopaedics and Sports Medicine** — RM musculoesquelética
- **Resnick – Diagnosis of Bone and Joint Disorders** — referência diagnóstica abrangente
- **Radiopaedia.org / ACR** — consenso radiológico atual

Região detectada: **{region_label}**
{multi_image_note}
Você recebeu (nesta ordem):
1. Atlas de referência em PDF (se fornecido) — base anatômica
2. Imagem(ns) de referência normal — padrão de comparação
3. Caso clínico validado por especialista (se fornecido) — exemplo de calibração de um laudo já revisado por profissional para o mesmo tipo de exame; NÃO é um achado deste paciente, use apenas como referência de padrão/rigor
4. Exame do paciente (imagem ou imagens finais) — objeto da análise

---

## REGRAS DE EVIDÊNCIA — LEIA ANTES DE ANALISAR

Classifique internamente cada achado antes de reportá-lo:

- **[FORTE]**: achado inequívoco, visível em pelo menos um plano com morfologia característica clara. Afirme como fato.
- **[MODERADO]**: achado provável, mas com limitação técnica ou sinal limítrofe. Afirme com qualificação objetiva.
- **[FRACO]**: achado possível mas não confirmável com a imagem disponível — **NÃO inclua na impressão diagnóstica**. Registre apenas em "Limitações" se relevante.

**Regra de ouro:** É melhor uma análise com 3 achados sólidos do que 8 achados diluídos por especulação. Priorize precisão sobre volume.

---

## 1. IDENTIFICAÇÃO DO EXAME
- Modalidade: RM / Raio-X / TC / US
- Região anatômica e lateralidade (D/E quando visível)
- Plano(s) / incidência(s) avaliados
- Qualidade técnica: adequada / limitada — e o que isso restringe na análise

## 2. ANÁLISE ESTRUTURAL — COMPARAÇÃO COM PADRÃO NORMAL
Compare sistematicamente cada estrutura relevante com o padrão normal (atlas + imagens de referência fornecidas).

**Ossos e articulações:**
- Alinhamento, eixos e relações articulares
- Densidade óssea / intensidade de sinal — edema, contusão, fratura, remodelação
- Superfícies articulares e amplitude do espaço articular

**Partes moles (RM/US):**
- Tendões: continuidade, espessura, sinal intersticial (grau I/II/III)
- Ligamentos: integridade, espessamento, rotura parcial ou total
- Meniscos / fibrocartilagem: morfologia, sinal, extensão da lesão
- Cartilagem articular: espessura, irregularidade, lesão focal (ICRS quando aplicável)
- Bursa e derrame articular: volume estimado, localização, sinal

## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA

Para cada achado identificado como [FORTE] ou [MODERADO]:
- Rótulo de evidência: **[FORTE]** ou **[MODERADO]**
- Localização anatômica precisa
- Extensão / dimensões estimadas
- Caracterização morfológica detalhada
- Classificação padronizada quando aplicável (Outerbridge, Kellgren-Lawrence, Anderson, Pfirrmann, etc.)

Ao final desta seção, inclua um parágrafo curto:
> **Achados descartados por evidência insuficiente:** [liste brevemente o que foi considerado mas não confirmado, e por quê — ex.: "possível edema subcondral focal no côndilo lateral descartado por artefato de movimento na sequência DP"] — ou "Nenhum achado descartado."

## 4. IMPRESSÃO DIAGNÓSTICA

Baseado exclusivamente nos achados [FORTE] e [MODERADO]:

- Diagnóstico(s) principal(is) em ordem de probabilidade
- Para cada diagnóstico: grau de certeza explícito — **confirmado pela imagem** / **provável** / **possível**
- Diagnósticos diferenciais relevantes com o critério que os diferencia neste exame

**Não inclua hipóteses baseadas em achados [FRACO]. Não especule além do que a imagem mostra.**

## 5. IMPACTO NA ESTRUTURA FÍSICA DO PACIENTE

Esta seção traduz os achados para o que eles significam concretamente para a pessoa.

**5a. O que está alterado e por quê importa:**
Explique, em linguagem clara mas tecnicamente precisa, o que cada achado principal representa anatomicamente e biomechanicamente — qual estrutura está comprometida, qual função ela desempenha, e como seu comprometimento afeta o sistema articular como um todo.

**5b. Consequências funcionais atuais:**
Com base nos achados, descreva o que a pessoa provavelmente experimenta: padrão de dor (mecânica/inflamatória/noturna), limitação de amplitude de movimento, instabilidade, déficit de força — correlacionando diretamente com cada achado.

**5c. Progressão provável sem intervenção:**
Descreva o curso natural esperado da condição identificada — estabilização, progressão lenta, risco de agravamento agudo (ex.: rotura completa a partir de parcial), degeneração articular progressiva — com base na literatura.

**5d. Fatores de risco estrutural:**
Identifique achados que representam vulnerabilidade adicional: estruturas adjacentes comprometidas, assimetrias biomecânicas, sinais de sobrecarga compensatória.

## 6. CONDUTA E PRÓXIMOS PASSOS

Use o histórico clínico fornecido (idade, sexo, atividade física, queixa, tempo de evolução, mecanismo, tratamentos anteriores) para personalizar esta seção. Se algum campo relevante não foi informado, indique o que mudaria na interpretação caso fosse conhecido.

- **Correlação com o quadro clínico:** como os achados de imagem explicam (ou não) os sintomas relatados
- **Exames complementares justificados** (artro-RM, SPECT-TC, ultrassom dinâmico, etc.) — somente se agregarem diagnóstico específico
- **Urgência: eletiva / prioritária / urgente** — com critério explícito baseado nos achados
- **Linha de tratamento sugerida** (conservador vs. cirúrgico): com base no perfil do paciente (idade, atividade, tempo de evolução) e nos achados — indique qual abordagem é mais indicada e por quê
- **Prognóstico funcional:** o que o paciente pode esperar com cada abordagem

---

Use terminologia ortopédica precisa em todo o relatório.
Seja direto: achados fortes são afirmados como fatos, não como possibilidades.
Achados moderados são qualificados com a limitação específica que os impede de ser fortes.
Achados fracos não aparecem no diagnóstico.

"""


def _process_image_bytes(raw_bytes: bytes) -> tuple[bytes, str]:
    """Valida e converte imagem para formato compatível. Retorna (bytes, mime_type)."""
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img_format = img.format or "JPEG"
        if img_format.upper() not in ["JPEG", "PNG", "WEBP", "GIF"]:
            buffer = io.BytesIO()
            img.convert("RGB").save(buffer, format="JPEG")
            raw_bytes = buffer.getvalue()
            img_format = "JPEG"
        mime_type = f"image/{img_format.lower()}"
    except Exception:
        mime_type = "image/jpeg"
    return raw_bytes, mime_type


def detect_exam_type_from_image(
    client: genai.Client,
    image_bytes: bytes,
    mime_type: str,
    model_name: str = "gemini-2.5-flash",
    deadline: float | None = None,
) -> str | None:
    """Detecta a região anatômica analisando visualmente a imagem via Gemini."""
    prompt = (
        "Você é um especialista em radiologia musculoesquelética com vasta experiência em RM, TC e Raio-X.\n\n"
        "TAREFA: Identifique a região anatômica mostrada neste exame de imagem.\n\n"
        "IMPORTANTE: A imagem pode ser uma foto tirada de uma tela de computador/monitor — isso é normal. "
        "Ignore artefatos de reflexo, moldura da tela, teclado ou interface do software de visualização. "
        "Foque exclusivamente nas estruturas anatômicas visíveis dentro da imagem médica.\n\n"
        "Guia de identificação:\n"
        "- joelho: fêmur distal, tíbia proximal, fíbula proximal, patela, meniscos, ligamentos cruzados\n"
        "- coluna: corpos vertebrais, discos intervertebrais, canal medular, processo espinhoso\n"
        "- ombro: cabeça do úmero, glenoide, acrômio, manguito rotador\n"
        "- quadril: cabeça femoral, acetábulo, colo do fêmur, articulação coxofemoral\n"
        "- pe_tornozelo: calcâneo, tálus, tíbia distal, metatarsos, tornozelo\n"
        "- mao_punho: carpo, metacarpos, falanges, rádio distal\n"
        "- cotovelo: úmero distal, rádio proximal, ulna proximal, olécrano\n"
        "- geral: use somente se for impossível identificar a região\n\n"
        "Responda APENAS com uma das palavras-chave acima (ex: joelho), sem mais nenhum texto."
    )
    try:
        response = _generate_with_retry(
            client,
            model_name,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
            max_output_tokens=16,
            deadline=deadline,
        )
        result = response.text.strip().lower()
        valid_types = {"joelho", "coluna", "ombro", "quadril", "pe_tornozelo", "mao_punho", "cotovelo", "geral"}
        for token in result.replace("\n", " ").split():
            token_clean = token.strip(".,;:-*")
            if token_clean in valid_types:
                logger.debug("Resposta bruta: '%s' → detectado: '%s'", result, token_clean)
                return token_clean
        logger.warning("Resposta de detecção visual não mapeável: '%s'", result)
        return None
    except Exception:
        logger.warning("Detecção visual do tipo de exame falhou.", exc_info=True)
        return None


def _build_content_parts(
    client: genai.Client,
    exam_images: list[tuple[bytes, str]],
    exam_type: str,
    user_description: str,
    reference_pdfs: list,
    reference_images: list,
    validated_case_text: str | None = None,
) -> tuple[list, int]:
    """Monta a lista de parts para envio ao Gemini."""
    parts = []
    refs_used = 0

    if reference_pdfs:
        parts.append(types.Part.from_text(
            text="**ATLAS DE REFERÊNCIA ANATÔMICA (use como base de conhecimento):**"
        ))
        for pdf_bytes, _ in reference_pdfs:
            result = _get_or_upload_pdf(client, pdf_bytes)
            if result:
                uri, mime = result
                parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime))
            else:
                parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
            refs_used += 1

    if reference_images:
        parts.append(types.Part.from_text(
            text="**IMAGENS DE REFERÊNCIA NORMAL (padrão visual de comparação):**"
        ))
        for i, (ref_bytes, ref_mime) in enumerate(reference_images):
            parts.append(types.Part.from_text(text=f"Referência {i + 1} — Anatomia normal:"))
            parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=ref_mime))
            refs_used += 1

    if validated_case_text:
        parts.append(types.Part.from_text(text=f"\n{validated_case_text}"))

    num_images = len(exam_images)
    if num_images == 1:
        parts.append(types.Part.from_text(text="\n**EXAME DO PACIENTE (imagem para análise):**"))
        parts.append(types.Part.from_bytes(data=exam_images[0][0], mime_type=exam_images[0][1]))
    else:
        parts.append(types.Part.from_text(
            text=f"\n**EXAME DO PACIENTE ({num_images} imagens para análise — analise todas em conjunto):**"
        ))
        for i, (img_bytes, img_mime) in enumerate(exam_images):
            parts.append(types.Part.from_text(text=f"Imagem {i + 1} do exame:"))
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=img_mime))

    if user_description:
        parts.append(types.Part.from_text(
            text=f"\n**HISTÓRICO CLÍNICO DO PACIENTE (use como contexto obrigatório nas seções 5 e 6):**\n{user_description}"
        ))

    parts.append(types.Part.from_text(text=build_analysis_prompt(exam_type, num_images)))

    return parts, refs_used


def _build_claude_content(
    exam_images: list[tuple[bytes, str]],
    exam_type: str,
    user_description: str,
    reference_images: list,
    validated_case_text: str | None = None,
) -> list:
    """Monta o conteúdo multimodal para a API do Claude."""
    content = []

    if reference_images:
        content.append({"type": "text", "text": "**IMAGENS DE REFERÊNCIA NORMAL (padrão visual de comparação):**"})
        for i, (ref_bytes, ref_mime) in enumerate(reference_images):
            content.append({"type": "text", "text": f"Referência {i + 1} — Anatomia normal:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": ref_mime,
                    "data": base64.standard_b64encode(ref_bytes).decode("utf-8"),
                },
            })

    if validated_case_text:
        content.append({"type": "text", "text": f"\n{validated_case_text}"})

    num_images = len(exam_images)
    if num_images == 1:
        content.append({"type": "text", "text": "\n**EXAME DO PACIENTE (imagem para análise):**"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": exam_images[0][1],
                "data": base64.standard_b64encode(exam_images[0][0]).decode("utf-8"),
            },
        })
    else:
        content.append({"type": "text", "text": f"\n**EXAME DO PACIENTE ({num_images} imagens — analise todas em conjunto):**"})
        for i, (img_bytes, img_mime) in enumerate(exam_images):
            content.append({"type": "text", "text": f"Imagem {i + 1} do exame:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img_mime,
                    "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
                },
            })

    if user_description:
        content.append({
            "type": "text",
            "text": f"\n**HISTÓRICO CLÍNICO DO PACIENTE (use como contexto obrigatório nas seções 5 e 6):**\n{user_description}",
        })

    content.append({"type": "text", "text": build_analysis_prompt(exam_type, num_images)})

    return content


def _analyze_with_claude(
    anthropic_api_key: str,
    exam_images: list[tuple[bytes, str]],
    exam_type: str,
    user_description: str,
    reference_images: list,
    timeout_seconds: float = _CLAUDE_ANALYSIS_TIMEOUT_SECONDS,
    validated_case_text: str | None = None,
) -> tuple[str, dict | None] | None:
    """Análise independente com Claude. Retorna (texto, uso_de_tokens) ou None em caso de falha."""
    try:
        logger.info("Claude: iniciando análise independente...")
        client = anthropic.Anthropic(
            api_key=anthropic_api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        content = _build_claude_content(
            exam_images, exam_type, user_description, reference_images, validated_case_text
        )
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=_CLAUDE_ANALYSIS_MAX_TOKENS,
            messages=[{"role": "user", "content": content}],
            timeout=timeout_seconds,
        )
        logger.info("Claude: análise concluída.")
        return message.content[0].text, _extract_claude_usage("claude-sonnet-4-6", message)
    except Exception:
        logger.warning("Claude: análise falhou.", exc_info=True)
        return None


def _synthesize_analyses(
    anthropic_api_key: str,
    gemini_analysis: str,
    claude_analysis: str,
    exam_type: str,
    user_description: str,
    timeout_seconds: float = _CLAUDE_SYNTHESIS_TIMEOUT_SECONDS,
) -> tuple[str, dict | None]:
    """
    Sintetiza as análises do Gemini e do Claude em um relatório de consenso final.
    Achados convergentes recebem maior peso; divergentes são mantidos com qualificação moderada.
    Retorna (texto, uso_de_tokens); uso_de_tokens é None se a síntese falhar
    (caso em que o texto retornado é a análise do Gemini, sem custo adicional).
    """
    region_map = {
        "joelho":       "Joelho",
        "coluna":       "Coluna Vertebral",
        "ombro":        "Ombro",
        "quadril":      "Quadril",
        "pe_tornozelo": "Pé e Tornozelo",
        "mao_punho":    "Mão e Punho",
        "cotovelo":     "Cotovelo",
        "geral":        "Região Ortopédica",
    }
    region_label = region_map.get(exam_type, "Região Ortopédica")

    synthesis_prompt = f"""Você é um especialista sênior em radiologia musculoesquelética da plataforma Three Health.

Dois sistemas de IA analisaram o mesmo exame de imagem de forma completamente independente. Sua tarefa é criar um **relatório de consenso final** que maximize a precisão diagnóstica, aproveitando o melhor de cada análise.

**Região anatômica:** {region_label}
**Histórico clínico:** {user_description or "Não informado"}

---

## ANÁLISE DO MODELO A (Gemini 2.5 Flash):
{_truncate_for_synthesis(gemini_analysis)}

---

## ANÁLISE DO MODELO B (Claude Sonnet):
{_truncate_for_synthesis(claude_analysis)}

---

## REGRAS DE SÍNTESE — APLIQUE COM RIGOR:

**1. Achados convergentes** (ambos identificaram o mesmo achado):
   - Se pelo menos um classificou como **[FORTE]**: elevar para **[FORTE]** no consenso.
   - Se ambos classificaram como **[MODERADO]**: manter como **[MODERADO]**, mas com descrição enriquecida.
   - A concordância de dois modelos independentes é forte evidência — seja assertivo.

**2. Achados divergentes** (apenas um modelo identificou):
   - Se o achado é anatomicamente plausível, bem localizado e descrito com detalhe: incluir como **[MODERADO]**.
   - Se é vago, especulativo ou contradiz o outro modelo diretamente: excluir, listando em "Achados descartados".

**3. Contradições diretas** (os modelos chegaram a conclusões opostas):
   - Adote a posição mais conservadora (menor comprometimento).
   - Descreva brevemente a discordância em "Achados descartados por evidência insuficiente".

**4. Completude:**
   - O relatório final deve ser mais completo e preciso que qualquer análise individual.
   - Combine descrições complementares do mesmo achado para enriquecer a caracterização.

**ESTRUTURA OBRIGATÓRIA DO RELATÓRIO FINAL** (siga exatamente, sem modificar os títulos):

## 1. IDENTIFICAÇÃO DO EXAME
## 2. ANÁLISE ESTRUTURAL — COMPARAÇÃO COM PADRÃO NORMAL
## 3. ACHADOS — CLASSIFICADOS POR FORÇA DE EVIDÊNCIA
## 4. IMPRESSÃO DIAGNÓSTICA
## 5. IMPACTO NA ESTRUTURA FÍSICA DO PACIENTE
## 6. CONDUTA E PRÓXIMOS PASSOS

**Regras finais:**
- Não invente achados que nenhum modelo identificou
- Seja completo, mas objetivo: priorize um laudo de até 1.200 palavras
- Use terminologia ortopédica precisa em todo o relatório
- Responda diretamente com o relatório — sem introdução, sem explicação do processo de síntese, sem menção aos "Modelos A e B"
- O resultado deve parecer um único laudo coeso escrito por um especialista humano sênior

RELATÓRIO DE CONSENSO FINAL:"""

    try:
        logger.info("Síntese: gerando relatório de consenso...")
        client = anthropic.Anthropic(
            api_key=anthropic_api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=_CLAUDE_SYNTHESIS_MAX_TOKENS,
            messages=[{"role": "user", "content": synthesis_prompt}],
            timeout=timeout_seconds,
        )
        logger.info("Síntese: relatório de consenso concluído.")
        return message.content[0].text, _extract_claude_usage("claude-sonnet-4-6", message)
    except Exception:
        logger.warning("Síntese falhou. Retornando análise Gemini.", exc_info=True)
        return gemini_analysis, None


def _run_dual_analysis(
    gemini_client: genai.Client,
    gemini_model: str,
    gemini_parts: list,
    anthropic_api_key: str,
    exam_images: list[tuple[bytes, str]],
    exam_type: str,
    user_description: str,
    reference_images: list,
    deadline: float | None = None,
    validated_case_text: str | None = None,
) -> tuple[str, str, list[dict]]:
    """
    Executa Gemini e Claude em paralelo.
    Retorna (gemini_text, claude_text_or_empty, uso_de_tokens).
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    gemini_text = ""
    claude_result = ""
    usage: list[dict] = []
    try:
        gemini_future = executor.submit(
            _generate_with_retry,
            gemini_client,
            gemini_model,
            gemini_parts,
            _GEMINI_MAX_OUTPUT_TOKENS,
            deadline,
        )
        claude_future = executor.submit(
            _analyze_with_claude,
            anthropic_api_key,
            exam_images,
            exam_type,
            user_description,
            reference_images,
            _remaining_seconds(deadline, _CLAUDE_ANALYSIS_TIMEOUT_SECONDS),
            validated_case_text,
        )

        done, pending = concurrent.futures.wait(
            {gemini_future, claude_future},
            timeout=_remaining_seconds(deadline, _ANALYSIS_BUDGET_SECONDS),
        )

        if gemini_future in done:
            try:
                gemini_response = gemini_future.result()
                gemini_text = gemini_response.text
                gemini_usage = _extract_gemini_usage(gemini_model, gemini_response)
                if gemini_usage:
                    usage.append(gemini_usage)
            except Exception:
                logger.warning("Dual AI: Gemini falhou.", exc_info=True)
        else:
            logger.warning("Dual AI: Gemini excedeu o orçamento de tempo; seguindo com fallback disponível.")
            gemini_future.cancel()

        if claude_future in done:
            try:
                claude_pair = claude_future.result()
                if claude_pair:
                    claude_result, claude_usage_item = claude_pair
                    if claude_usage_item:
                        usage.append(claude_usage_item)
            except Exception:
                logger.warning("Dual AI: Claude falhou.", exc_info=True)
        else:
            logger.warning("Dual AI: Claude excedeu o orçamento de tempo; seguindo sem análise independente.")
            claude_future.cancel()

        for future in pending:
            future.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return gemini_text, (claude_result or ""), usage


def _produce_final_analysis(
    gemini_client: genai.Client,
    model_name: str,
    content_parts: list,
    anthropic_api_key: str,
    exam_images: list[tuple[bytes, str]],
    exam_type: str,
    user_description: str,
    reference_images: list,
    deadline: float | None,
    validated_case_text: str | None,
) -> tuple[str, str, list[dict]]:
    """
    Executa a análise (dual Gemini+Claude quando anthropic_api_key é fornecida,
    senão apenas Gemini) e retorna (texto_final, modelo_usado, uso_de_tokens).
    """
    if not anthropic_api_key:
        response = _generate_with_retry(gemini_client, model_name, contents=content_parts, deadline=deadline)
        usage = []
        gemini_usage = _extract_gemini_usage(model_name, response)
        if gemini_usage:
            usage.append(gemini_usage)
        return response.text, model_name, usage

    logger.info("Dual AI: executando Gemini + Claude em paralelo...")
    gemini_text, claude_text, usage = _run_dual_analysis(
        gemini_client, model_name, content_parts,
        anthropic_api_key, exam_images, exam_type, user_description, reference_images,
        deadline, validated_case_text,
    )
    if gemini_text and claude_text and not _deadline_expired(deadline, reserve_seconds=12):
        final_analysis, synthesis_usage = _synthesize_analyses(
            anthropic_api_key, gemini_text, claude_text, exam_type, user_description,
            timeout_seconds=_remaining_seconds(deadline, _CLAUDE_SYNTHESIS_TIMEOUT_SECONDS),
        )
        if synthesis_usage:
            usage.append(synthesis_usage)
        model_used = f"{model_name} + claude-sonnet-4-6 → consenso"
    elif gemini_text:
        final_analysis = gemini_text
        model_used = f"{model_name} → consenso rápido"
    elif claude_text:
        final_analysis = claude_text
        model_used = "claude-sonnet-4-6 → consenso rápido"
    else:
        raise TimeoutError("A análise excedeu o limite de tempo antes de gerar um laudo.")
    return final_analysis, model_used, usage


def analyze_exam(
    exam_image_paths: list[str] | str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
    anthropic_api_key: str = "",
) -> dict:
    """
    Analisa exames médicos com Gemini + Claude em paralelo (quando chave Anthropic disponível),
    gerando um relatório de consenso que maximiza a precisão diagnóstica.

    Args:
        exam_image_paths: Caminho ou lista de caminhos para as imagens
        api_key: Chave da API do Gemini
        user_description: Descrição clínica fornecida pelo usuário
        model_name: Modelo Gemini a utilizar
        anthropic_api_key: Chave da API da Anthropic (habilita análise dual)
    """
    if isinstance(exam_image_paths, str):
        exam_image_paths = [exam_image_paths]

    exam_images = []
    for path in exam_image_paths:
        with open(path, "rb") as f:
            raw_bytes = f.read()
        processed_bytes, mime_type = _process_image_bytes(raw_bytes)
        exam_images.append((processed_bytes, mime_type))

    cache_key = _analysis_cache_key(
        [data for data, _ in exam_images], user_description, model_name, bool(anthropic_api_key)
    )
    cached_result = _get_cached_analysis(cache_key)
    if cached_result:
        return cached_result

    deadline = time.monotonic() + _ANALYSIS_BUDGET_SECONDS
    gemini_client = _make_gemini_client(api_key)

    first_filename = Path(exam_image_paths[0]).name
    keyword_type = detect_exam_type(first_filename, user_description)
    if keyword_type != "geral":
        exam_type = keyword_type
        logger.debug("Tipo de exame detectado por keywords: %s", exam_type)
    else:
        visual_type = detect_exam_type_from_image(
            gemini_client, exam_images[0][0], exam_images[0][1], model_name, deadline
        ) if exam_images and not _deadline_expired(deadline, reserve_seconds=70) else None
        if visual_type and visual_type != "geral":
            exam_type = visual_type
            logger.debug("Tipo de exame detectado visualmente: %s", exam_type)
        else:
            exam_type = keyword_type
            logger.debug("Tipo de exame detectado por fallback: %s", exam_type)

    reference_pdfs = get_reference_pdfs() if _INCLUDE_REFERENCE_PDF else []
    reference_images = get_reference_images_as_bytes(exam_type)
    validated_case_text = _format_validated_case_reference(
        db.buscar_caso_validado_referencia(exam_type)
    )

    content_parts, refs_used = _build_content_parts(
        gemini_client, exam_images, exam_type, user_description,
        reference_pdfs, reference_images, validated_case_text,
    )

    final_analysis, model_used, usage = _produce_final_analysis(
        gemini_client, model_name, content_parts, anthropic_api_key,
        exam_images, exam_type, user_description, reference_images,
        deadline, validated_case_text,
    )

    result = {
        "success": True,
        "exam_type": exam_type,
        "analysis": final_analysis,
        "references_used": refs_used,
        "model_used": model_used,
        "num_images": len(exam_images),
        "usage": usage,
        "cache_hit": False,
    }
    _store_cached_analysis(cache_key, result)
    return result


def analyze_exam_from_bytes(
    exam_images_data: list[tuple[bytes, str]] | tuple[bytes, str],
    exam_filename: str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
    anthropic_api_key: str = "",
) -> dict:
    """
    Analisa exames médicos diretamente dos bytes das imagens.
    Suporta análise dual Gemini+Claude quando anthropic_api_key é fornecido.

    Args:
        exam_images_data: Tupla única (bytes, mime) ou lista de tuplas
        exam_filename: Nome do arquivo principal (para detecção do tipo)
        api_key: Chave da API do Gemini
        user_description: Descrição clínica fornecida pelo usuário
        model_name: Modelo Gemini a utilizar
        anthropic_api_key: Chave da API da Anthropic (habilita análise dual)
    """
    if isinstance(exam_images_data, tuple) and len(exam_images_data) == 2 and isinstance(exam_images_data[0], bytes):
        exam_images_data = [exam_images_data]

    exam_images = []
    for raw_bytes, _ in exam_images_data:
        processed_bytes, mime_type = _process_image_bytes(raw_bytes)
        exam_images.append((processed_bytes, mime_type))

    cache_key = _analysis_cache_key(
        [data for data, _ in exam_images], user_description, model_name, bool(anthropic_api_key)
    )
    cached_result = _get_cached_analysis(cache_key)
    if cached_result:
        return cached_result

    deadline = time.monotonic() + _ANALYSIS_BUDGET_SECONDS
    gemini_client = _make_gemini_client(api_key)

    keyword_type = detect_exam_type(exam_filename, user_description)
    if keyword_type != "geral":
        exam_type = keyword_type
        logger.debug("Tipo de exame detectado por keywords: %s", exam_type)
    else:
        visual_type = detect_exam_type_from_image(
            gemini_client, exam_images[0][0], exam_images[0][1], model_name, deadline
        ) if exam_images and not _deadline_expired(deadline, reserve_seconds=70) else None
        if visual_type and visual_type != "geral":
            exam_type = visual_type
            logger.debug("Tipo de exame detectado visualmente: %s", exam_type)
        else:
            exam_type = keyword_type
            logger.debug("Tipo de exame detectado por fallback: %s", exam_type)

    reference_pdfs = get_reference_pdfs() if _INCLUDE_REFERENCE_PDF else []
    reference_images = get_reference_images_as_bytes(exam_type)
    validated_case_text = _format_validated_case_reference(
        db.buscar_caso_validado_referencia(exam_type)
    )

    content_parts, refs_used = _build_content_parts(
        gemini_client, exam_images, exam_type, user_description,
        reference_pdfs, reference_images, validated_case_text,
    )

    final_analysis, model_used, usage = _produce_final_analysis(
        gemini_client, model_name, content_parts, anthropic_api_key,
        exam_images, exam_type, user_description, reference_images,
        deadline, validated_case_text,
    )

    result = {
        "success": True,
        "exam_type": exam_type,
        "analysis": final_analysis,
        "references_used": refs_used,
        "model_used": model_used,
        "num_images": len(exam_images),
        "usage": usage,
        "cache_hit": False,
    }
    _store_cached_analysis(cache_key, result)
    return result
