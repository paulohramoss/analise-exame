"""
Módulo principal de análise de exames médicos usando a API do Gemini.
Compara o exame enviado com imagens de referência normais e gera um laudo.
"""

import io
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from core.reference_images import (
    detect_exam_type,
    get_reference_images_as_bytes,
)


def build_analysis_prompt(exam_type: str) -> str:
    """Constrói o prompt especializado para análise médica."""
    return f"""
Você é um assistente de análise de imagens médicas especializado.
Sua função é auxiliar médicos na interpretação de exames de imagem.

**AVISO IMPORTANTE:** Esta análise é uma ferramenta de suporte e NÃO substitui
o diagnóstico de um médico especialista. Sempre consulte um profissional de saúde.

Tipo de exame detectado: {exam_type.replace("_", " ").title()}

Você recebeu:
1. Uma imagem de exame do paciente (última imagem enviada)
2. Imagem(ns) de referência de exames normais (imagens anteriores)

Por favor, realize uma análise comparativa detalhada seguindo esta estrutura:

## 1. IDENTIFICAÇÃO DO EXAME
- Tipo de exame e região anatômica
- Qualidade técnica da imagem
- Plano/corte da imagem (quando aplicável)

## 2. COMPARAÇÃO COM PADRÃO NORMAL
- Estruturas que apresentam aspecto normal
- Diferenças observadas em relação ao padrão de referência
- Alterações de sinal, densidade, forma ou tamanho (se houver)

## 3. ACHADOS PRINCIPAIS
- Liste as principais alterações identificadas
- Localização precisa de cada achado
- Características das alterações (dimensões estimadas, bordas, intensidade)

## 4. IMPRESSÃO DIAGNÓSTICA
- Hipóteses diagnósticas em ordem de probabilidade
- Correlação com possíveis condições clínicas
- Grau de certeza dos achados

## 5. RECOMENDAÇÕES
- Exames complementares sugeridos (se necessário)
- Urgência de avaliação médica (baixa/média/alta)
- Observações adicionais relevantes

Seja preciso, objetivo e utilize terminologia médica adequada.
Indique claramente quando há limitações na análise.
"""


def analyze_exam(
    exam_image_path: str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
) -> dict:
    """
    Analisa um exame médico comparando com imagens de referência normais.

    Args:
        exam_image_path: Caminho para a imagem do exame a ser analisado
        api_key: Chave da API do Gemini
        user_description: Descrição adicional fornecida pelo usuário
        model_name: Modelo Gemini a utilizar

    Returns:
        Dicionário com resultado da análise e metadados
    """
    client = genai.Client(api_key=api_key)

    # Detecta o tipo de exame
    filename = Path(exam_image_path).name
    exam_type = detect_exam_type(filename, user_description)

    # Carrega imagem do exame do paciente
    with open(exam_image_path, "rb") as f:
        exam_image_bytes = f.read()

    # Verifica e converte a imagem para JPEG se necessário
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

    # Busca imagens de referência normais
    reference_images = get_reference_images_as_bytes(exam_type)

    # Monta o conteúdo para envio ao Gemini
    content_parts = []

    # Adiciona imagens de referência primeiro
    if reference_images:
        content_parts.append(types.Part.from_text(text="**IMAGENS DE REFERÊNCIA (exames normais para comparação):**"))
        for i, (ref_bytes, ref_mime) in enumerate(reference_images):
            content_parts.append(types.Part.from_text(text=f"Referência {i + 1} - Exame normal:"))
            content_parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=ref_mime))

    # Adiciona imagem do paciente
    content_parts.append(types.Part.from_text(text="\n**EXAME DO PACIENTE (imagem para análise):**"))
    content_parts.append(types.Part.from_bytes(data=exam_image_bytes, mime_type=mime_type))

    # Adiciona descrição do usuário se fornecida
    if user_description:
        content_parts.append(types.Part.from_text(text=f"\n**Informações adicionais do solicitante:** {user_description}"))

    # Adiciona o prompt de análise
    content_parts.append(types.Part.from_text(text=build_analysis_prompt(exam_type)))

    # Gera a análise
    response = client.models.generate_content(model=model_name, contents=content_parts)

    return {
        "success": True,
        "exam_type": exam_type,
        "analysis": response.text,
        "references_used": len(reference_images),
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

    # Verifica e converte imagem
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

    reference_images = get_reference_images_as_bytes(exam_type)

    content_parts = []

    if reference_images:
        content_parts.append(types.Part.from_text(text="**IMAGENS DE REFERÊNCIA (exames normais para comparação):**"))
        for i, (ref_bytes, ref_mime) in enumerate(reference_images):
            content_parts.append(types.Part.from_text(text=f"Referência {i + 1} - Exame normal:"))
            content_parts.append(types.Part.from_bytes(data=ref_bytes, mime_type=ref_mime))

    content_parts.append(types.Part.from_text(text="\n**EXAME DO PACIENTE (imagem para análise):**"))
    content_parts.append(types.Part.from_bytes(data=exam_image_bytes, mime_type=mime_type))

    if user_description:
        content_parts.append(types.Part.from_text(text=f"\n**Informações adicionais:** {user_description}"))

    content_parts.append(types.Part.from_text(text=build_analysis_prompt(exam_type)))

    response = client.models.generate_content(model=model_name, contents=content_parts)

    return {
        "success": True,
        "exam_type": exam_type,
        "analysis": response.text,
        "references_used": len(reference_images),
        "model_used": model_name,
    }
