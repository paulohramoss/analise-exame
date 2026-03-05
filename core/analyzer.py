"""
Módulo principal de análise de exames médicos usando a API do Gemini.
Compara o exame enviado com atlas em PDF e imagens de referência normais.

PDFs são enviados via Gemini File API (upload na primeira chamada, URI cacheado
em memória por 47h para não re-enviar o arquivo a cada análise).
"""

import hashlib
import io
import os
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from core.reference_images import (
    detect_exam_type,
    get_reference_images_as_bytes,
    get_reference_pdfs,
)

# Cache in-memory: {md5_hash: {"uri": str, "mime": str, "expires_at": float}}
# Persiste enquanto o container Vercel estiver ativo (evita re-upload a cada request)
_pdf_uri_cache: dict = {}


def _get_or_upload_pdf(client: genai.Client, pdf_bytes: bytes) -> tuple[str, str] | None:
    """
    Faz upload do PDF para a Gemini File API na primeira chamada e cacheia o URI.
    Retorna (uri, mime_type) ou None se o upload falhar.
    O arquivo expira em 48h; o cache é invalidado após 47h.
    """
    pdf_hash = hashlib.md5(pdf_bytes).hexdigest()

    cached = _pdf_uri_cache.get(pdf_hash)
    if cached and cached["expires_at"] > time.time():
        print(f"[PDF cache] Reutilizando URI: {cached['uri'][:60]}...")
        return cached["uri"], cached["mime"]

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        print(f"[PDF upload] Enviando {len(pdf_bytes)//1024} KB para Gemini File API...")
        file_response = client.files.upload(file=tmp_path)

        uri = file_response.uri
        mime = file_response.mime_type or "application/pdf"

        _pdf_uri_cache[pdf_hash] = {
            "uri": uri,
            "mime": mime,
            "expires_at": time.time() + 47 * 3600,
        }
        print(f"[PDF upload] Concluído. URI: {uri[:60]}...")
        return uri, mime

    except Exception as e:
        print(f"[PDF upload] Falhou: {e}. Usando bytes inline como fallback.")
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def build_analysis_prompt(exam_type: str) -> str:
    """Constrói o prompt especializado para análise médica."""
    return f"""
Você é um assistente especializado em análise de imagens médicas da plataforma Three Health.
Sua função é auxiliar profissionais de saúde na interpretação de exames de imagem.

**BASE DE CONHECIMENTO:**
Sua análise deve ser fundamentada nas referências anatômicas e de imagem mais consagradas:
- **Atlas(es) fornecido(s) acima** — use como referência primária de anatomia normal
- **Gray's Anatomy** (41ª edição) — anatomia topográfica e relações estruturais
- **Netter's Atlas of Human Anatomy** — referência visual de estruturas anatômicas normais
- **Fundamentals of Diagnostic Radiology** (Brant & Helms) — padrões radiológicos normais e patológicos
- **Radiopaedia.org** — casos clínicos com consenso radiológico
- **ACR Appropriateness Criteria** — critérios de adequação do American College of Radiology

**AVISO IMPORTANTE:** Esta análise é uma ferramenta de suporte educacional e de apoio.
NÃO substitui o diagnóstico de um médico especialista.
Sempre consulte um radiologista ou médico qualificado.

Tipo de exame detectado: {exam_type.replace("_", " ").title()}

Você recebeu (nesta ordem):
1. Atlas/livro de referência anatômica em PDF (se enviado) — base de conhecimento
2. Imagem(ns) de referência de exame normal — padrão visual de comparação
3. O exame do paciente (última imagem) — objeto da análise

Realize uma análise comparativa estruturada e detalhada:

## 1. IDENTIFICAÇÃO DO EXAME
- Modalidade de imagem e região anatômica
- Qualidade técnica (ruído, contraste, artefatos)
- Plano/corte/incidência (quando aplicável)

## 2. ANÁLISE ANATÔMICA — COMPARAÇÃO COM PADRÃO NORMAL
Com base no atlas de referência e nas imagens normais fornecidas:
- Estruturas visíveis e seus aspectos normais esperados
- Desvios identificados em relação ao padrão de referência
- Alterações de sinal (RM), densidade (TC/Rx), ecogenicidade (US)
- Alterações morfológicas: forma, tamanho, bordas, contornos

## 3. ACHADOS PRINCIPAIS
- Liste cada achado com localização anatômica precisa
- Dimensões estimadas quando possível
- Características semiológicas (ex.: hipointensidade T2, hiperdensidade, calcificação)
- Diferencie achados incidentais de potencialmente patológicos

## 4. IMPRESSÃO DIAGNÓSTICA
- Hipóteses diagnósticas em ordem de probabilidade
- Correlação anatômica e fisiopatológica de cada hipótese
- Grau de confiança: alto / moderado / baixo (com justificativa)

## 5. RECOMENDAÇÕES
- Correlação clínica necessária
- Exames complementares sugeridos (com justificativa)
- Urgência de avaliação: eletiva / prioritária / urgente
- Referências anatômicas relevantes para o caso

Use terminologia médica precisa (SNOMED/RadLex quando aplicável).
Seja objetivo e indique explicitamente as limitações da análise por IA.
"""


def _build_content_parts(
    client: genai.Client,
    exam_image_bytes: bytes,
    mime_type: str,
    exam_type: str,
    user_description: str,
    reference_pdfs: list,
    reference_images: list,
) -> tuple[list, int]:
    """
    Monta a lista de parts para envio ao Gemini.
    Retorna (content_parts, total_references_used).
    """
    parts = []
    refs_used = 0

    # 1. Atlas em PDF via File API (ou inline como fallback)
    if reference_pdfs:
        parts.append(types.Part.from_text(
            text="**ATLAS DE REFERÊNCIA ANATÔMICA (use como base de conhecimento):**"
        ))
        for pdf_bytes, _ in reference_pdfs:
            result = _get_or_upload_pdf(client, pdf_bytes)
            if result:
                uri, mime = result
                parts.append(types.Part.from_uri(uri=uri, mime_type=mime))
            else:
                # Fallback inline se File API falhar (somente se < 20MB)
                parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
            refs_used += 1

    # 2. Imagens de referência de exame normal
    if reference_images:
        parts.append(types.Part.from_text(
            text="**IMAGENS DE REFERÊNCIA NORMAL (padrão visual de comparação):**"
        ))
        for i, (ref_bytes, ref_mime) in enumerate(reference_images):
            parts.append(types.Part.from_text(text=f"Referência {i + 1} — Anatomia normal:"))
            parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=ref_mime))
            refs_used += 1

    # 3. Exame do paciente
    parts.append(types.Part.from_text(text="\n**EXAME DO PACIENTE (imagem para análise):**"))
    parts.append(types.Part.from_bytes(data=exam_image_bytes, mime_type=mime_type))

    # 4. Contexto clínico
    if user_description:
        parts.append(types.Part.from_text(
            text=f"\n**Contexto clínico fornecido:** {user_description}"
        ))

    # 5. Prompt de análise
    parts.append(types.Part.from_text(text=build_analysis_prompt(exam_type)))

    return parts, refs_used


def analyze_exam(
    exam_image_path: str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
) -> dict:
    """
    Analisa um exame médico comparando com atlas em PDF e imagens de referência normais.

    Args:
        exam_image_path: Caminho para a imagem do exame a ser analisado
        api_key: Chave da API do Gemini
        user_description: Descrição adicional fornecida pelo usuário
        model_name: Modelo Gemini a utilizar

    Returns:
        Dicionário com resultado da análise e metadados
    """
    client = genai.Client(api_key=api_key)

    filename = Path(exam_image_path).name
    exam_type = detect_exam_type(filename, user_description)

    with open(exam_image_path, "rb") as f:
        exam_image_bytes = f.read()

    try:
        img = Image.open(io.BytesIO(exam_image_bytes))
        img_format = img.format or "JPEG"
        if img_format.upper() not in ["JPEG", "PNG", "WEBP", "GIF"]:
            buffer = io.BytesIO()
            img.convert("RGB").save(buffer, format="JPEG")
            exam_image_bytes = buffer.getvalue()
            img_format = "JPEG"
        mime_type = f"image/{img_format.lower()}"
    except Exception:
        mime_type = "image/jpeg"

    reference_pdfs = get_reference_pdfs()
    reference_images = get_reference_images_as_bytes(exam_type)

    content_parts, refs_used = _build_content_parts(
        client, exam_image_bytes, mime_type, exam_type, user_description,
        reference_pdfs, reference_images,
    )

    response = client.models.generate_content(model=model_name, contents=content_parts)

    return {
        "success": True,
        "exam_type": exam_type,
        "analysis": response.text,
        "references_used": refs_used,
        "model_used": model_name,
    }


def analyze_exam_from_bytes(
    exam_image_bytes: bytes,
    exam_filename: str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
) -> dict:
    """
    Analisa um exame médico diretamente dos bytes da imagem.
    Versão alternativa que não requer salvar o arquivo primeiro.
    """
    client = genai.Client(api_key=api_key)

    exam_type = detect_exam_type(exam_filename, user_description)

    try:
        img = Image.open(io.BytesIO(exam_image_bytes))
        img_format = img.format or "JPEG"
        if img_format.upper() not in ["JPEG", "PNG", "WEBP"]:
            buffer = io.BytesIO()
            img.convert("RGB").save(buffer, format="JPEG")
            exam_image_bytes = buffer.getvalue()
            img_format = "JPEG"
        mime_type = f"image/{img_format.lower()}"
    except Exception:
        mime_type = "image/jpeg"

    reference_pdfs = get_reference_pdfs()
    reference_images = get_reference_images_as_bytes(exam_type)

    content_parts, refs_used = _build_content_parts(
        client, exam_image_bytes, mime_type, exam_type, user_description,
        reference_pdfs, reference_images,
    )

    response = client.models.generate_content(model=model_name, contents=content_parts)

    return {
        "success": True,
        "exam_type": exam_type,
        "analysis": response.text,
        "references_used": refs_used,
        "model_used": model_name,
    }
