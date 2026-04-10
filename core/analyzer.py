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

# Modelos de fallback em ordem de preferência
_FALLBACK_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash"]

# Erros que justificam retry (sobrecarga / indisponibilidade temporária)
_RETRYABLE_CODES = {503, 429, 500}
_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # segundos (dobra a cada tentativa)


def _is_retryable(exc: Exception) -> bool:
    """Retorna True se a exceção indica sobrecarga temporária e merece retry."""
    msg = str(exc).lower()
    return (
        "503" in msg
        or "unavailable" in msg
        or "429" in msg
        or "rate limit" in msg
        or "resource_exhausted" in msg
        or "overloaded" in msg
        or "quota" in msg
        or "500" in msg
    )


def _generate_with_retry(client: genai.Client, model_name: str, contents) -> object:
    """
    Chama generate_content com retry exponencial.
    Se o modelo principal falhar por 503/sobrecarga, tenta modelos de fallback.
    """
    models_to_try = [model_name] + [m for m in _FALLBACK_MODELS if m != model_name]
    last_exc = None

    for model in models_to_try:
        delay = _RETRY_DELAY
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(model=model, contents=contents)
                if model != model_name:
                    print(f"[retry] Resposta obtida com modelo de fallback: {model}")
                return response
            except Exception as exc:
                last_exc = exc
                if _is_retryable(exc):
                    if attempt < _MAX_RETRIES:
                        print(f"[retry] Tentativa {attempt}/{_MAX_RETRIES} falhou ({exc}). Aguardando {delay:.1f}s...")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        print(f"[retry] Modelo {model} esgotou {_MAX_RETRIES} tentativas. Tentando fallback...")
                        break  # tenta próximo modelo
                else:
                    raise  # erro não recuperável (auth, payload inválido, etc.)

    raise last_exc

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
3. Exame do paciente (última(s) imagem(ns)) — objeto da análise

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
) -> str | None:
    """
    Detecta a região anatômica do exame analisando visualmente a imagem via Gemini.
    Retorna uma das chaves de EXAM_TYPE_KEYWORDS ou None se não conseguir determinar.
    """
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
        )
        result = response.text.strip().lower()
        valid_types = {"joelho", "coluna", "ombro", "quadril", "pe_tornozelo", "mao_punho", "cotovelo", "geral"}
        # Extrai o primeiro token válido da resposta (tolerância a respostas com espaços/pontuação)
        for token in result.replace("\n", " ").split():
            token_clean = token.strip(".,;:-*")
            if token_clean in valid_types:
                print(f"[detect_exam_type_from_image] Resposta bruta: '{result}' → detectado: '{token_clean}'")
                return token_clean
        print(f"[detect_exam_type_from_image] Resposta não mapeável: '{result}'")
        return None
    except Exception as e:
        print(f"[detect_exam_type_from_image] Falhou: {e}")
        return None


def _build_content_parts(
    client: genai.Client,
    exam_images: list[tuple[bytes, str]],
    exam_type: str,
    user_description: str,
    reference_pdfs: list,
    reference_images: list,
) -> tuple[list, int]:
    """
    Monta a lista de parts para envio ao Gemini.
    exam_images: lista de tuplas (bytes, mime_type) das imagens do exame do paciente.
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

    # 3. Exame(s) do paciente
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

    # 4. Contexto clínico
    if user_description:
        parts.append(types.Part.from_text(
            text=f"\n**HISTÓRICO CLÍNICO DO PACIENTE (use como contexto obrigatório nas seções 5 e 6):**\n{user_description}"
        ))

    # 5. Prompt de análise
    parts.append(types.Part.from_text(text=build_analysis_prompt(exam_type, num_images)))

    return parts, refs_used


def analyze_exam(
    exam_image_paths: list[str] | str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
) -> dict:
    """
    Analisa um ou mais exames médicos comparando com atlas em PDF e imagens de referência normais.

    Args:
        exam_image_paths: Caminho (str) ou lista de caminhos para as imagens do exame
        api_key: Chave da API do Gemini
        user_description: Descrição adicional fornecida pelo usuário
        model_name: Modelo Gemini a utilizar

    Returns:
        Dicionário com resultado da análise e metadados
    """
    # Aceita tanto string única quanto lista para compatibilidade
    if isinstance(exam_image_paths, str):
        exam_image_paths = [exam_image_paths]

    client = genai.Client(api_key=api_key)

    # Processa todas as imagens
    exam_images = []
    for path in exam_image_paths:
        with open(path, "rb") as f:
            raw_bytes = f.read()
        processed_bytes, mime_type = _process_image_bytes(raw_bytes)
        exam_images.append((processed_bytes, mime_type))

    # Detecção do tipo de exame: visual (IA) tem prioridade; keywords apenas como fallback
    first_filename = Path(exam_image_paths[0]).name
    visual_type = detect_exam_type_from_image(client, exam_images[0][0], exam_images[0][1], model_name) if exam_images else None
    if visual_type and visual_type != "geral":
        exam_type = visual_type
        print(f"[detect_exam_type] Tipo detectado visualmente: {exam_type}")
    else:
        exam_type = detect_exam_type(first_filename, user_description)
        print(f"[detect_exam_type] Tipo detectado por keywords: {exam_type}")

    reference_pdfs = get_reference_pdfs()
    reference_images = get_reference_images_as_bytes(exam_type)

    content_parts, refs_used = _build_content_parts(
        client, exam_images, exam_type, user_description,
        reference_pdfs, reference_images,
    )

    response = _generate_with_retry(client, model_name, contents=content_parts)
    model_used = model_name  # pode ter mudado para fallback, mas não há como saber aqui

    return {
        "success": True,
        "exam_type": exam_type,
        "analysis": response.text,
        "references_used": refs_used,
        "model_used": model_used,
        "num_images": len(exam_images),
    }


def analyze_exam_from_bytes(
    exam_images_data: list[tuple[bytes, str]] | tuple[bytes, str],
    exam_filename: str,
    api_key: str,
    user_description: str = "",
    model_name: str = "gemini-2.5-flash",
) -> dict:
    """
    Analisa um ou mais exames médicos diretamente dos bytes das imagens.

    Args:
        exam_images_data: Tupla única (bytes, filename) ou lista de tuplas para múltiplas imagens
        exam_filename: Nome do arquivo principal (para detecção do tipo de exame)
        api_key: Chave da API do Gemini
        user_description: Descrição adicional fornecida pelo usuário
        model_name: Modelo Gemini a utilizar
    """
    # Aceita tupla única ou lista de tuplas
    if isinstance(exam_images_data, tuple) and len(exam_images_data) == 2 and isinstance(exam_images_data[0], bytes):
        exam_images_data = [exam_images_data]

    client = genai.Client(api_key=api_key)

    exam_images = []
    for raw_bytes, _ in exam_images_data:
        processed_bytes, mime_type = _process_image_bytes(raw_bytes)
        exam_images.append((processed_bytes, mime_type))

    # Detecção do tipo de exame: visual (IA) tem prioridade; keywords apenas como fallback
    visual_type = detect_exam_type_from_image(client, exam_images[0][0], exam_images[0][1], model_name) if exam_images else None
    if visual_type and visual_type != "geral":
        exam_type = visual_type
        print(f"[detect_exam_type] Tipo detectado visualmente: {exam_type}")
    else:
        exam_type = detect_exam_type(exam_filename, user_description)
        print(f"[detect_exam_type] Tipo detectado por keywords: {exam_type}")

    reference_pdfs = get_reference_pdfs()
    reference_images = get_reference_images_as_bytes(exam_type)

    content_parts, refs_used = _build_content_parts(
        client, exam_images, exam_type, user_description,
        reference_pdfs, reference_images,
    )

    response = _generate_with_retry(client, model_name, contents=content_parts)

    return {
        "success": True,
        "exam_type": exam_type,
        "analysis": response.text,
        "references_used": refs_used,
        "model_used": model_name,
        "num_images": len(exam_images),
    }
