"""
Integração com Supabase — Three Health Platform.

Todas as operações são fire-and-forget: erros são logados mas nunca propagados
para não bloquear o fluxo principal da aplicação.

As consultas assumem o schema de supabase_migrations/schema.sql completo (sem
"tentar de novo sem as colunas novas"): rode as migrations antes do deploy.
"""

import hashlib
import importlib
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

from core import patient_crypto
from core.ttl_cache import TTLCache
from collections import Counter

logger = logging.getLogger(__name__)

# Cache em memória do melhor caso validado por tipo de exame, usado como
# exemplo de calibração no prompt de análise. TTL curto para não bater no
# Supabase a cada análise, mas ainda refletir novas validações em minutos.
_VALIDATED_CASE_CACHE_TTL_SECONDS = 900
_validated_case_cache = TTLCache(maxsize=64, ttl_seconds=_VALIDATED_CASE_CACHE_TTL_SECONDS)
_NO_CASE = object()


@lru_cache(maxsize=1)
def _get_client():
    """Retorna cliente Supabase (singleton). None se não configurado."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        logger.warning("SUPABASE_URL ou SUPABASE_SERVICE_KEY não configurados — persistência desativada.")
        return None
    try:
        supabase_module = importlib.import_module("supabase")
        create_client = getattr(supabase_module, "create_client")
        client = create_client(url, key)
        logger.info("Conectado ao Supabase.")
        return client
    except Exception:
        logger.exception("Falha ao criar cliente Supabase")
        return None


def is_configured() -> bool:
    """True se há cliente Supabase (persistência ativa)."""
    return _get_client() is not None


def health_check() -> bool:
    """True se o Supabase está configurado e responde a uma query mínima agora."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("clientes").select("id").limit(1).execute()
        return True
    except Exception:
        logger.warning("Health check do Supabase falhou.", exc_info=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLIENTES
# ─────────────────────────────────────────────────────────────────────────────

def upsert_cliente(nome: str, email: str, cpf_cnpj: str = "", asaas_customer_id: str = "") -> str | None:
    """Cria ou atualiza cliente pelo e-mail. Retorna UUID ou None."""
    db = _get_client()
    if not db:
        return None
    try:
        data: dict = {"nome": nome, "email": email}
        if cpf_cnpj:
            data["cpf_cnpj"] = cpf_cnpj
        if asaas_customer_id:
            data["asaas_customer_id"] = asaas_customer_id
        res = db.table("clientes").upsert(data, on_conflict="email").execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em upsert_cliente")
        return None


def buscar_cliente_id_por_email(email: str) -> str | None:
    """Retorna UUID do cliente pelo e-mail, ou None se não encontrado."""
    db = _get_client()
    if not db or not email:
        return None
    try:
        res = db.table("clientes").select("id").eq("email", email).limit(1).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em buscar_cliente_id_por_email")
        return None


def listar_clientes(limite: int = 1000) -> list[dict]:
    """Lista clientes (id, nome, email, created_at) para auditoria. [] em erro."""
    db = _get_client()
    if not db:
        return []
    try:
        res = (
            db.table("clientes")
            .select("id,nome,email,created_at")
            .order("created_at", desc=False)
            .limit(limite)
            .execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_clientes")
        return []


def buscar_sessao_cliente(email: str) -> dict | None:
    """Retorna {"id", "sessao_versao"} do cliente, ou None se não encontrado/sem banco.

    sessao_versao é gravada no cookie premium; incrementá-la (troca de senha,
    estorno) invalida todos os cookies já emitidos para o cliente.
    """
    db = _get_client()
    if not db or not email:
        return None
    try:
        res = db.table("clientes").select("id, sessao_versao").eq("email", email).limit(1).execute()
    except Exception:
        # Banco sem a migration 2026-10-13-sessao-versao.sql: ainda resolve o id.
        logger.warning("Coluna clientes.sessao_versao indisponível; revogação de sessão desativada.", exc_info=True)
        cliente_id = buscar_cliente_id_por_email(email)
        return {"id": cliente_id, "sessao_versao": 0} if cliente_id else None
    if not res.data:
        return None
    row = res.data[0]
    return {"id": row["id"], "sessao_versao": int(row.get("sessao_versao") or 0)}


def revogar_sessoes_cliente(email: str) -> bool:
    """Invalida todos os cookies premium do cliente (incrementa sessao_versao)."""
    db = _get_client()
    if not db or not email:
        return False
    try:
        sessao = buscar_sessao_cliente(email)
        if not sessao:
            return False
        db.table("clientes").update({"sessao_versao": sessao["sessao_versao"] + 1}).eq("email", email).execute()
        return True
    except Exception:
        logger.exception("Erro em revogar_sessoes_cliente")
        return False


def salvar_senha_cliente(email: str, senha_hash: str) -> bool:
    """Salva o hash bcrypt da senha de acesso do cliente. Retorna True se bem-sucedido."""
    db = _get_client()
    if not db or not email:
        return False
    try:
        db.table("clientes").update({"senha_hash": senha_hash}).eq("email", email).execute()
        return True
    except Exception:
        logger.exception("Erro em salvar_senha_cliente")
        return False


def buscar_senha_hash_cliente(email: str) -> str | None:
    """Retorna o hash bcrypt da senha do cliente, ou None se não definida."""
    db = _get_client()
    if not db or not email:
        return None
    try:
        res = (
            db.table("clientes")
            .select("senha_hash")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0].get("senha_hash")
        return None
    except Exception:
        logger.exception("Erro em buscar_senha_hash_cliente")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PAGAMENTOS
# ─────────────────────────────────────────────────────────────────────────────

def salvar_pagamento(
    asaas_payment_id: str,
    valor: float,
    descricao: str = "",
    invoice_url: str = "",
    external_reference: str = "",
    cliente_id: str | None = None,
    payload: dict | None = None,
    plano: str | None = None,
    forma_pagamento: str | None = None,
) -> str | None:
    """Insere ou atualiza pagamento pelo ID Asaas. Retorna UUID ou None."""
    db = _get_client()
    if not db:
        return None
    try:
        data: dict = {
            "asaas_payment_id": asaas_payment_id,
            "valor": valor,
            "descricao": descricao or None,
            "invoice_url": invoice_url or None,
            "external_reference": external_reference or None,
            "cliente_id": cliente_id,
            "payload_asaas": payload,
            "plano": plano or None,
            "forma_pagamento": forma_pagamento or None,
        }
        res = db.table("pagamentos").upsert(data, on_conflict="asaas_payment_id").execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_pagamento")
        return None


def buscar_pagamento_confirmado_por_email(
    email: str,
    planos: tuple[str, ...] = ("mensal", "semestral", "anual"),
) -> tuple[str, str, str | None] | None:
    """Retorna (asaas_payment_id, external_reference, inicio_iso) do pagamento confirmado
    mais recente do e-mail, ou None.
    Suporta external_reference no formato 'email' (legado) ou 'email|plano' (atual).
    `inicio_iso` é a data a partir da qual o plano conta (ver _inicio_do_plano).

    Compara por igualdade exata contra as referências possíveis. Um `ilike`
    com prefixo deixava `%` e `_` digitados pelo usuário agirem como curinga
    (ex.: `%@%` casava com qualquer pagamento confirmado).
    """
    db = _get_client()
    if not db or not email:
        return None
    referencias = [email] + [f"{email}|{plano}" for plano in planos]
    try:
        res = (
            db.table("pagamentos")
            .select("asaas_payment_id, external_reference, created_at, payload_asaas")
            .in_("external_reference", referencias)
            .in_("status", ["CONFIRMED", "RECEIVED"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            return row["asaas_payment_id"], row.get("external_reference", ""), _inicio_do_plano(row)
        return None
    except Exception:
        logger.exception("Erro em buscar_pagamento_confirmado_por_email")
        return None


def _inicio_do_plano(row: dict) -> str | None:
    """Data de confirmação do pagamento (Asaas), com fallback para a criação da cobrança.

    payload_asaas é o objeto da cobrança (gravado no checkout) ou o corpo do
    webhook ({"event", "payment": {...}}), conforme o que chegou por último.
    """
    payload = row.get("payload_asaas") or {}
    if isinstance(payload, dict):
        payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else payload
        for campo in ("confirmedDate", "clientPaymentDate", "paymentDate"):
            if payment.get(campo):
                return str(payment[campo])
    return row.get("created_at")


def buscar_pagamento_id(asaas_payment_id: str) -> str | None:
    """Retorna UUID interno do pagamento pelo ID do Asaas, ou None."""
    db = _get_client()
    if not db or not asaas_payment_id:
        return None
    try:
        res = db.table("pagamentos").select("id").eq("asaas_payment_id", asaas_payment_id).limit(1).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em buscar_pagamento_id")
        return None


def atualizar_status_pagamento(asaas_payment_id: str, status: str, payload: dict | None = None) -> None:
    """Atualiza o status de um pagamento pelo ID do Asaas."""
    db = _get_client()
    if not db:
        return
    try:
        data: dict = {"status": status}
        if payload:
            data["payload_asaas"] = payload
        db.table("pagamentos").update(data).eq("asaas_payment_id", asaas_payment_id).execute()
    except Exception:
        logger.exception("Erro em atualizar_status_pagamento")


# ─────────────────────────────────────────────────────────────────────────────
# SESSÕES DE ACESSO
# ─────────────────────────────────────────────────────────────────────────────

def salvar_sessao(
    cliente_id: str | None,
    pagamento_id: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    """Registra sessão premium emitida (cookie). Retorna UUID ou None."""
    db = _get_client()
    if not db:
        return None
    try:
        res = db.table("sessoes_acesso").insert({
            "cliente_id": cliente_id,
            "pagamento_id": pagamento_id,
            "ip_address": ip_address,
            "user_agent": user_agent,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_sessao")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISES
# ─────────────────────────────────────────────────────────────────────────────

def salvar_analise(
    tipo_exame: str,
    analise_completa: str,
    modelo_ia: str,
    referencias_usadas: int,
    num_imagens: int,
    modo: str,
    descricao_usuario: str = "",
    modalidade: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    tempo_ms: int | None = None,
    cliente_id: str | None = None,
    sessao_id: str | None = None,
    responsavel: dict | None = None,
    paciente_id: str | None = None,
    marcacoes: list[dict] | None = None,
) -> str | None:
    """Persiste resultado de análise da IA. Retorna UUID da análise ou None.

    paciente_id já deve ter sido conferido como do mesmo cliente_id (rota).
    """
    db = _get_client()
    if not db:
        return None
    try:
        data = {
            "tipo_exame": tipo_exame,
            "analise_completa": analise_completa,
            "modelo_ia": modelo_ia,
            "referencias_usadas": referencias_usadas,
            "num_imagens": num_imagens,
            "modo": modo,
            "descricao_usuario": descricao_usuario or None,
            "modalidade": modalidade,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "tempo_processamento_ms": tempo_ms,
            "cliente_id": cliente_id,
            "sessao_id": sessao_id,
        }
        if paciente_id:
            data["paciente_id"] = paciente_id
        if marcacoes:
            data["marcacoes"] = marcacoes
        if responsavel:
            data.update({
                "responsavel_nome": responsavel.get("name") or None,
                "responsavel_perfil": responsavel.get("role") or None,
                "responsavel_registro": responsavel.get("register") or None,
                "responsavel_instituicao": responsavel.get("organization") or None,
            })
        res = db.table("analises").insert(data).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_analise")
        return None


def listar_analises(
    cliente_id: str | None = None,
    limit: int = 50,
    include_all: bool = False,
) -> list[dict]:
    """Lista análises salvas para o cliente atual. Admin pode listar todas."""
    db = _get_client()
    if not db:
        return []
    if not include_all and not cliente_id:
        return []

    columns = (
        "id, created_at, tipo_exame, modelo_ia, referencias_usadas, num_imagens, "
        "modo, descricao_usuario, modalidade, tempo_processamento_ms, paciente_id, "
        "responsavel_nome, responsavel_perfil, responsavel_registro, responsavel_instituicao"
    )
    try:
        query = db.table("analises").select(columns)
        if cliente_id:
            query = query.eq("cliente_id", cliente_id)
        res = query.order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_analises")
        return []


def buscar_analise(
    analise_id: str,
    cliente_id: str | None = None,
    include_all: bool = False,
) -> dict | None:
    """Busca uma análise, respeitando o vínculo com o cliente quando não for admin."""
    db = _get_client()
    if not db or not analise_id:
        return None
    if not include_all and not cliente_id:
        return None

    columns = (
        "id, created_at, tipo_exame, analise_completa, modelo_ia, referencias_usadas, "
        "num_imagens, modo, descricao_usuario, modalidade, tempo_processamento_ms, cliente_id, paciente_id, marcacoes, "
        "responsavel_nome, responsavel_perfil, responsavel_registro, responsavel_instituicao"
    )
    try:
        query = db.table("analises").select(columns).eq("id", analise_id)
        if cliente_id:
            query = query.eq("cliente_id", cliente_id)
        res = query.limit(1).execute()
        return res.data[0] if res.data else None
    except Exception:
        logger.exception("Erro em buscar_analise")
        return None


def salvar_imagem_exame(
    analise_id: str,
    mime_type: str,
    tamanho_bytes: int,
    hash_md5: str,
    ordem: int = 1,
    origem: str = "upload",
    arquivo_original: str = "",
    dicom_metadata: dict | None = None,
) -> None:
    """Persiste metadados de imagem de exame vinculada a uma análise."""
    db = _get_client()
    if not db:
        return
    try:
        db.table("imagens_exame").insert({
            "analise_id": analise_id,
            "mime_type": mime_type,
            "tamanho_bytes": tamanho_bytes,
            "hash_md5": hash_md5,
            "ordem": ordem,
            "origem": origem or "upload",
            "arquivo_original": arquivo_original or None,
            "dicom_metadata": dicom_metadata or None,
        }).execute()
    except Exception:
        logger.exception("Erro em salvar_imagem_exame")


def salvar_analise_com_imagens(
    result: dict,
    modo: str,
    images: list[tuple[bytes, str]],
    image_meta: list[dict],
    tempo_ms: int | None = None,
    **persist,
) -> str | None:
    """Persiste o resultado de analyze_exam_from_bytes e os metadados de cada imagem.

    Ponto único usado pelas rotas síncronas e pelo worker da fila (antes eram
    quatro cópias do mesmo bloco). `persist` são os campos opcionais de
    salvar_analise (descricao_usuario, modalidade, ip_address, user_agent,
    cliente_id, responsavel, sessao_id). Retorna o UUID da análise ou None.
    """
    analise_id = salvar_analise(
        tipo_exame=result["exam_type"],
        analise_completa=result["analysis"],
        modelo_ia=result["model_used"],
        referencias_usadas=result["references_used"],
        num_imagens=result.get("num_images", len(images)),
        modo=modo,
        tempo_ms=tempo_ms,
        marcacoes=result.get("annotations") or None,
        **persist,
    )
    if not analise_id:
        return None
    for ordem, (raw_bytes, mime) in enumerate(images, 1):
        meta = image_meta[ordem - 1] if ordem - 1 < len(image_meta) else {}
        salvar_imagem_exame(
            analise_id=analise_id,
            mime_type=mime,
            tamanho_bytes=len(raw_bytes),
            hash_md5=hashlib.md5(raw_bytes).hexdigest(),
            ordem=ordem,
            origem=meta.get("origem") or "upload",
            arquivo_original=meta.get("arquivo_original") or "",
            dicom_metadata=meta.get("dicom_metadata") or None,
        )
    return analise_id


def listar_feedbacks_por_analises(analise_ids: list[str]) -> list[dict]:
    """Lista feedbacks vinculados às análises informadas."""
    db = _get_client()
    if not db or not analise_ids:
        return []
    try:
        res = (
            db.table("feedbacks_ia")
            .select("id, analise_id, tipo, secao, fonte, fonte_nome, created_at")
            .in_("analise_id", analise_ids)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_feedbacks_por_analises")
        return []


def listar_validacoes_clinicas(analise_ids: list[str]) -> list[dict]:
    """Lista validações clínicas formais vinculadas às análises informadas."""
    db = _get_client()
    if not db or not analise_ids:
        return []
    try:
        res = (
            db.table("diagnosticos_validados")
            .select(
                "id, analise_id, tipo_exame, modalidade, concordancia, grau_concordancia, "
                "achados_corretos, achados_perdidos, achados_incorretos, validado_por, created_at"
            )
            .in_("analise_id", analise_ids)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_validacoes_clinicas")
        return []


def buscar_caso_validado_referencia(tipo_exame: str, min_concordancia: int = 80) -> dict | None:
    """
    Retorna o diagnóstico validado mais recente do tipo de exame com
    grau_concordancia >= min_concordancia e aprovado_para_referencia = true,
    para uso como exemplo de calibração no prompt de análise. Cacheado em
    memória por _VALIDATED_CASE_CACHE_TTL_SECONDS (inclusive resultados vazios).

    A aprovação é manual (admin, direto no Supabase): o texto da validação é
    livre e escrito por qualquer cliente premium, e entra no prompt das
    análises de TODOS os clientes — sem curadoria, abria espaço para injeção
    de instruções no modelo e vazava dados clínicos entre clientes.
    """
    cached = _validated_case_cache.get(tipo_exame, _NO_CASE)
    if cached is not _NO_CASE:
        return cached

    case = None
    db = _get_client()
    if db:
        try:
            res = (
                db.table("diagnosticos_validados")
                .select(
                    "diagnostico_final, achados_corretos, achados_perdidos, "
                    "achados_incorretos, grau_concordancia, created_at"
                )
                .eq("tipo_exame", tipo_exame)
                .eq("aprovado_para_referencia", True)
                .gte("grau_concordancia", min_concordancia)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                case = res.data[0]
        except Exception:
            logger.exception("Erro em buscar_caso_validado_referencia")

    _validated_case_cache.set(tipo_exame, case)
    return case


def montar_metricas_dashboard(
    analises: list[dict],
    validacoes: list[dict],
    feedbacks: list[dict],
) -> dict:
    """Calcula métricas simples para dashboard de clínica."""
    total_analises = len(analises)
    total_validacoes = len(validacoes)
    validados = {v.get("analise_id") for v in validacoes if v.get("analise_id")}
    pendentes = max(total_analises - len(validados), 0)
    consenso_dual = sum(
        1
        for item in analises
        if "claude" in (item.get("modelo_ia") or "").lower()
        and "consenso" in (item.get("modelo_ia") or "").lower()
    )
    feedback_count = len(feedbacks)
    concordancias = [
        int(v.get("grau_concordancia") or 0)
        for v in validacoes
        if v.get("grau_concordancia") is not None
    ]
    media_concordancia = round(sum(concordancias) / len(concordancias)) if concordancias else 0
    concordancia_plena = sum(1 for v in validacoes if v.get("concordancia") is True)
    exames_por_tipo = Counter(item.get("tipo_exame") or "geral" for item in analises)
    modalidades = Counter(item.get("modalidade") or "Não informada" for item in analises)

    return {
        "total_analises": total_analises,
        "total_validacoes": total_validacoes,
        "pendentes_validacao": pendentes,
        "feedbacks": feedback_count,
        "consenso_dual": consenso_dual,
        "consenso_percentual": round((consenso_dual / total_analises) * 100) if total_analises else 0,
        "media_concordancia": media_concordancia,
        "concordancia_plena": concordancia_plena,
        "exames_por_tipo": exames_por_tipo.most_common(6),
        "modalidades": modalidades.most_common(6),
    }


# ─────────────────────────────────────────────────────────────────────────────
# FEEDBACK / APRENDIZADO DA IA
# ─────────────────────────────────────────────────────────────────────────────

def salvar_feedback(
    analise_id: str,
    tipo: str,
    comentario: str = "",
    achado_original: str = "",
    achado_corrigido: str = "",
    secao: str | None = None,
    fonte: str = "paciente",
    fonte_nome: str = "",
) -> str | None:
    """
    Salva feedback/correção de um diagnóstico da IA.
    tipo: 'correcao' | 'validacao' | 'comentario' | 'falso_positivo' | 'falso_negativo'
    fonte: 'paciente' | 'medico' | 'radiologista' | 'sistema' | 'admin'
    """
    db = _get_client()
    if not db:
        return None
    try:
        res = db.table("feedbacks_ia").insert({
            "analise_id": analise_id,
            "tipo": tipo,
            "secao": secao,
            "achado_original": achado_original or None,
            "achado_corrigido": achado_corrigido or None,
            "comentario": comentario or None,
            "fonte": fonte,
            "fonte_nome": fonte_nome or None,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_feedback")
        return None


def salvar_diagnostico_validado(
    analise_id: str,
    tipo_exame: str,
    diagnostico_ia: str,
    diagnostico_final: str,
    concordancia: bool,
    grau_concordancia: int = 0,
    achados_corretos: list | None = None,
    achados_perdidos: list | None = None,
    achados_incorretos: list | None = None,
    validado_por: str = "",
    modalidade: str | None = None,
) -> str | None:
    """
    Registra diagnóstico validado por médico — base de treino supervisionado.
    grau_concordancia: 0-100 (% de concordância com a IA)
    """
    db = _get_client()
    if not db:
        return None
    try:
        res = db.table("diagnosticos_validados").insert({
            "analise_id": analise_id,
            "tipo_exame": tipo_exame,
            "modalidade": modalidade,
            "diagnostico_ia": diagnostico_ia,
            "diagnostico_final": diagnostico_final,
            "concordancia": concordancia,
            "grau_concordancia": grau_concordancia,
            "achados_corretos": achados_corretos or [],
            "achados_perdidos": achados_perdidos or [],
            "achados_incorretos": achados_incorretos or [],
            "validado_por": validado_por or None,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_diagnostico_validado")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PRONTUÁRIO — pacientes, tratamentos e evolução
#
# Todo acesso filtra por cliente_id: cada profissional só enxerga os próprios
# pacientes (não há visão admin). Nome e CPF ficam cifrados (core/patient_crypto).
# ─────────────────────────────────────────────────────────────────────────────

_PACIENTE_COLUMNS = (
    "id, cliente_id, nome_cifrado, cpf_cifrado, data_nascimento, sexo, observacoes, "
    "consentimento_em, created_at, updated_at"
)

PACIENTE_DUPLICADO = "duplicado"
PACIENTE_INDISPONIVEL = "indisponivel"


def _paciente_from_row(row: dict) -> dict:
    """Linha do banco → paciente com nome/CPF em claro (as colunas cifradas saem)."""
    cpf = patient_crypto.decrypt(row.get("cpf_cifrado"))
    paciente = {k: v for k, v in row.items() if k not in {"nome_cifrado", "cpf_cifrado"}}
    paciente.update({
        "nome": patient_crypto.decrypt(row.get("nome_cifrado")),
        "cpf": patient_crypto.format_cpf(cpf),
        "cpf_mascarado": patient_crypto.mask_cpf(cpf),
    })
    return paciente


def _cpf_ja_cadastrado(db, cliente_id: str, cpf_hash: str, exceto_id: str | None = None) -> bool:
    query = db.table("pacientes").select("id").eq("cliente_id", cliente_id).eq("cpf_hash", cpf_hash)
    if exceto_id:
        query = query.neq("id", exceto_id)
    return bool(query.limit(1).execute().data)


def criar_paciente(
    cliente_id: str,
    nome: str,
    cpf: str,
    data_nascimento: str | None = None,
    sexo: str | None = None,
    observacoes: str = "",
) -> tuple[str | None, str | None]:
    """Cadastra o paciente. Retorna (id, None) ou (None, PACIENTE_DUPLICADO | PACIENTE_INDISPONIVEL)."""
    db = _get_client()
    if not db or not cliente_id or not patient_crypto.is_configured():
        return None, PACIENTE_INDISPONIVEL
    try:
        cpf_hash = patient_crypto.cpf_hash(cpf)
        if _cpf_ja_cadastrado(db, cliente_id, cpf_hash):
            return None, PACIENTE_DUPLICADO
        res = db.table("pacientes").insert({
            "cliente_id": cliente_id,
            "nome_cifrado": patient_crypto.encrypt(nome),
            "cpf_cifrado": patient_crypto.encrypt(patient_crypto.cpf_digits(cpf)),
            "cpf_hash": cpf_hash,
            "data_nascimento": data_nascimento or None,
            "sexo": sexo or None,
            "observacoes": observacoes or None,
        }).execute()
        if not res.data:
            return None, PACIENTE_INDISPONIVEL
        return res.data[0]["id"], None
    except Exception:
        logger.exception("Erro em criar_paciente")
        return None, PACIENTE_INDISPONIVEL


def atualizar_paciente(
    paciente_id: str,
    cliente_id: str,
    nome: str,
    cpf: str,
    data_nascimento: str | None = None,
    sexo: str | None = None,
    observacoes: str = "",
) -> str | None:
    """Atualiza o cadastro. Retorna None se deu certo, ou o código do erro."""
    db = _get_client()
    if not db or not cliente_id or not patient_crypto.is_configured():
        return PACIENTE_INDISPONIVEL
    try:
        cpf_hash = patient_crypto.cpf_hash(cpf)
        if _cpf_ja_cadastrado(db, cliente_id, cpf_hash, exceto_id=paciente_id):
            return PACIENTE_DUPLICADO
        res = (
            db.table("pacientes").update({
                "nome_cifrado": patient_crypto.encrypt(nome),
                "cpf_cifrado": patient_crypto.encrypt(patient_crypto.cpf_digits(cpf)),
                "cpf_hash": cpf_hash,
                "data_nascimento": data_nascimento or None,
                "sexo": sexo or None,
                "observacoes": observacoes or None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", paciente_id).eq("cliente_id", cliente_id).execute()
        )
        return None if res.data else PACIENTE_INDISPONIVEL
    except Exception:
        logger.exception("Erro em atualizar_paciente")
        return PACIENTE_INDISPONIVEL


def listar_pacientes(cliente_id: str | None, limit: int = 1000) -> list[dict]:
    """Pacientes do profissional, em ordem alfabética (o nome só existe decifrado)."""
    db = _get_client()
    if not db or not cliente_id:
        return []
    try:
        res = (
            db.table("pacientes").select(_PACIENTE_COLUMNS)
            .eq("cliente_id", cliente_id).order("created_at", desc=True).limit(limit).execute()
        )
        pacientes = [_paciente_from_row(row) for row in res.data or []]
        return sorted(pacientes, key=lambda p: p["nome"].casefold())
    except Exception:
        logger.exception("Erro em listar_pacientes")
        return []


def buscar_paciente(paciente_id: str, cliente_id: str | None) -> dict | None:
    """Paciente do profissional, ou None (inexistente ou de outro profissional)."""
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return None
    try:
        res = (
            db.table("pacientes").select(_PACIENTE_COLUMNS)
            .eq("id", paciente_id).eq("cliente_id", cliente_id).limit(1).execute()
        )
        return _paciente_from_row(res.data[0]) if res.data else None
    except Exception:
        logger.exception("Erro em buscar_paciente")
        return None


def excluir_paciente(paciente_id: str, cliente_id: str) -> bool:
    """Apaga o paciente, tratamentos e evoluções (cascata). Os laudos ficam, sem vínculo."""
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return False
    try:
        res = db.table("pacientes").delete().eq("id", paciente_id).eq("cliente_id", cliente_id).execute()
        return bool(res.data)
    except Exception:
        logger.exception("Erro em excluir_paciente")
        return False


def listar_analises_paciente(paciente_id: str, cliente_id: str) -> list[dict]:
    """Laudos vinculados ao paciente, do mais antigo para o mais recente."""
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return []
    try:
        res = (
            db.table("analises")
            .select("id, created_at, tipo_exame, modalidade, modelo_ia, num_imagens, descricao_usuario")
            .eq("paciente_id", paciente_id).eq("cliente_id", cliente_id)
            .order("created_at").execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_analises_paciente")
        return []


def vincular_analise_paciente(analise_id: str, paciente_id: str | None, cliente_id: str) -> bool:
    """Liga (ou, com paciente_id=None, desliga) um laudo do profissional a um paciente dele."""
    db = _get_client()
    if not db or not analise_id or not cliente_id:
        return False
    try:
        res = (
            db.table("analises").update({"paciente_id": paciente_id})
            .eq("id", analise_id).eq("cliente_id", cliente_id).execute()
        )
        return bool(res.data)
    except Exception:
        logger.exception("Erro em vincular_analise_paciente")
        return False


def salvar_tratamento(
    paciente_id: str,
    cliente_id: str,
    tipo: str,
    data_inicio: str,
    descricao: str = "",
    regiao: str = "",
    data_fim: str | None = None,
) -> str | None:
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return None
    try:
        res = db.table("tratamentos").insert({
            "paciente_id": paciente_id,
            "cliente_id": cliente_id,
            "tipo": tipo,
            "descricao": descricao or None,
            "regiao": regiao or None,
            "data_inicio": data_inicio,
            "data_fim": data_fim or None,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_tratamento")
        return None


def encerrar_tratamento(tratamento_id: str, paciente_id: str, cliente_id: str, data_fim: str) -> bool:
    db = _get_client()
    if not db or not tratamento_id or not cliente_id:
        return False
    try:
        res = (
            db.table("tratamentos").update({"data_fim": data_fim, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", tratamento_id).eq("paciente_id", paciente_id).eq("cliente_id", cliente_id).execute()
        )
        return bool(res.data)
    except Exception:
        logger.exception("Erro em encerrar_tratamento")
        return False


def listar_tratamentos(paciente_id: str, cliente_id: str) -> list[dict]:
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return []
    try:
        res = (
            db.table("tratamentos")
            .select("id, tipo, descricao, regiao, data_inicio, data_fim, created_at")
            .eq("paciente_id", paciente_id).eq("cliente_id", cliente_id)
            .order("data_inicio").execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_tratamentos")
        return []


def salvar_evolucao(
    paciente_id: str,
    cliente_id: str,
    data: str,
    dor_eva: int | None = None,
    funcao: int | None = None,
    nota: str = "",
) -> str | None:
    """Registro de evolução: dor (EVA 0-10), função (0-100, maior = melhor) e nota livre."""
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return None
    try:
        res = db.table("evolucoes").insert({
            "paciente_id": paciente_id,
            "cliente_id": cliente_id,
            "data": data,
            "dor_eva": dor_eva,
            "funcao": funcao,
            "nota": nota or None,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception:
        logger.exception("Erro em salvar_evolucao")
        return None


def listar_evolucoes(paciente_id: str, cliente_id: str) -> list[dict]:
    db = _get_client()
    if not db or not paciente_id or not cliente_id:
        return []
    try:
        res = (
            db.table("evolucoes")
            .select("id, data, dor_eva, funcao, nota, created_at")
            .eq("paciente_id", paciente_id).eq("cliente_id", cliente_id)
            .order("data").order("created_at").execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Erro em listar_evolucoes")
        return []


def registrar_acesso_prontuario(cliente_id: str | None, paciente_id: str | None, acao: str) -> None:
    """Trilha de auditoria: quem abriu ou alterou qual prontuário, e quando."""
    db = _get_client()
    if not db or not cliente_id:
        return
    try:
        db.table("acessos_prontuario").insert({
            "cliente_id": cliente_id,
            "paciente_id": paciente_id,
            "acao": acao,
        }).execute()
    except Exception:
        logger.exception("Erro em registrar_acesso_prontuario")


# ─────────────────────────────────────────────────────────────────────────────
# DIREITOS DO TITULAR (LGPD art. 18)
# ─────────────────────────────────────────────────────────────────────────────

def exportar_dados_cliente(cliente_id: str) -> dict | None:
    """Todos os dados pessoais do cliente, para entrega ao titular (portabilidade/acesso).

    Fica de fora só o que é segredo do sistema (hash de senha, versão de sessão)
    e o payload bruto das cobranças (dados do gateway, não do titular).
    """
    db = _get_client()
    if not db or not cliente_id:
        return None
    try:
        cliente = (
            db.table("clientes").select("id, nome, email, cpf_cnpj, created_at")
            .eq("id", cliente_id).limit(1).execute().data
        )
        if not cliente:
            return None
        analises = (
            db.table("analises").select(
                "id, created_at, tipo_exame, modalidade, descricao_usuario, analise_completa, modelo_ia, "
                "num_imagens, responsavel_nome, responsavel_perfil, responsavel_registro, responsavel_instituicao"
            ).eq("cliente_id", cliente_id).order("created_at", desc=True).execute().data or []
        )
        ids = [a["id"] for a in analises]
        return {
            "cliente": cliente[0],
            "pagamentos": (
                db.table("pagamentos").select(
                    "asaas_payment_id, valor, descricao, plano, forma_pagamento, status, created_at"
                ).eq("cliente_id", cliente_id).order("created_at", desc=True).execute().data or []
            ),
            "analises": analises,
            "imagens_exame": (
                db.table("imagens_exame").select(
                    "analise_id, mime_type, tamanho_bytes, ordem, origem, arquivo_original, created_at"
                ).in_("analise_id", ids).execute().data or []
            ) if ids else [],
            "feedbacks": (
                db.table("feedbacks_ia").select("analise_id, tipo, comentario, fonte_nome, created_at")
                .in_("analise_id", ids).execute().data or []
            ) if ids else [],
            "validacoes_clinicas": (
                db.table("diagnosticos_validados").select(
                    "analise_id, diagnostico_final, grau_concordancia, validado_por, created_at"
                ).in_("analise_id", ids).execute().data or []
            ) if ids else [],
            "sessoes": (
                db.table("sessoes_acesso").select("ip_address, user_agent, created_at")
                .eq("cliente_id", cliente_id).order("created_at", desc=True).execute().data or []
            ),
            "pacientes": _exportar_prontuarios(db, cliente_id),
        }
    except Exception:
        logger.exception("Erro em exportar_dados_cliente")
        return None


def _exportar_prontuarios(db, cliente_id: str) -> list[dict]:
    """Pacientes do profissional (nome/CPF em claro), cada um com tratamentos e evoluções."""
    pacientes = [
        _paciente_from_row(row) for row in (
            db.table("pacientes").select(_PACIENTE_COLUMNS).eq("cliente_id", cliente_id).execute().data or []
        )
    ]
    tratamentos = db.table("tratamentos").select(
        "paciente_id, tipo, descricao, regiao, data_inicio, data_fim, created_at"
    ).eq("cliente_id", cliente_id).execute().data or []
    evolucoes = db.table("evolucoes").select(
        "paciente_id, data, dor_eva, funcao, nota, created_at"
    ).eq("cliente_id", cliente_id).execute().data or []
    for paciente in pacientes:
        paciente.pop("cliente_id", None)
        paciente.pop("cpf_mascarado", None)
        paciente["tratamentos"] = [t for t in tratamentos if t["paciente_id"] == paciente["id"]]
        paciente["evolucoes"] = [e for e in evolucoes if e["paciente_id"] == paciente["id"]]
    return pacientes


def marcar_exportacao_dados(cliente_id: str) -> None:
    """Registra quando o profissional baixou a cópia dos dados (inclui os prontuários)."""
    db = _get_client()
    if not db or not cliente_id:
        return
    try:
        db.table("clientes").update(
            {"dados_exportados_em": datetime.now(timezone.utc).isoformat()}
        ).eq("id", cliente_id).execute()
    except Exception:
        logger.exception("Erro em marcar_exportacao_dados")


def _mais_recente(db, tabela: str, coluna: str, cliente_id: str) -> str | None:
    rows = (
        db.table(tabela).select(coluna).eq("cliente_id", cliente_id)
        .order(coluna, desc=True).limit(1).execute().data or []
    )
    return rows[0][coluna] if rows else None


def resumo_backup_prontuarios(cliente_id: str) -> dict | None:
    """Quantos pacientes o profissional tem e se a última cópia baixada cobre tudo.

    `copia_em_dia` é False quando nunca houve exportação ou quando algum
    prontuário mudou depois dela: excluir a conta nesse estado apaga dados
    que o profissional não tem guardados. None se o banco não respondeu.
    """
    db = _get_client()
    if not db or not cliente_id:
        return None
    try:
        pacientes = len(
            db.table("pacientes").select("id").eq("cliente_id", cliente_id).execute().data or []
        )
        cliente = (
            db.table("clientes").select("dados_exportados_em").eq("id", cliente_id).limit(1).execute().data or [{}]
        )[0]
        exportado_em = cliente.get("dados_exportados_em")
        alteracoes = [
            _mais_recente(db, "pacientes", "updated_at", cliente_id),
            _mais_recente(db, "tratamentos", "updated_at", cliente_id),
            _mais_recente(db, "evolucoes", "created_at", cliente_id),
        ]
        ultima = max((_parse_ts(a) for a in alteracoes if a), default=None)
        exportado = _parse_ts(exportado_em) if exportado_em else None
        return {
            "pacientes": pacientes,
            "exportado_em": exportado_em,
            "copia_em_dia": pacientes == 0 or (exportado is not None and (ultima is None or exportado >= ultima)),
        }
    except Exception:
        logger.exception("Erro em resumo_backup_prontuarios")
        return None


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def excluir_dados_cliente(cliente_id: str) -> bool:
    """Elimina os dados pessoais do cliente (LGPD art. 18, VI).

    Apaga laudos (e, em cascata, imagens, feedbacks e validações), prontuários
    dos pacientes (com tratamentos e evoluções), sessões, logs de acesso e o
    cadastro. As cobranças em `pagamentos` permanecem — sem
    vínculo com o cadastro (cliente_id vira NULL pela FK) — porque registros
    fiscais têm guarda obrigatória por lei (LGPD art. 16, I).
    """
    db = _get_client()
    if not db or not cliente_id:
        return False
    try:
        db.table("analises").delete().eq("cliente_id", cliente_id).execute()
        db.table("pacientes").delete().eq("cliente_id", cliente_id).execute()
        db.table("acessos_prontuario").delete().eq("cliente_id", cliente_id).execute()
        db.table("sessoes_acesso").delete().eq("cliente_id", cliente_id).execute()
        db.table("logs_acesso").delete().eq("cliente_id", cliente_id).execute()
        db.table("clientes").delete().eq("id", cliente_id).execute()
        return True
    except Exception:
        logger.exception("Erro em excluir_dados_cliente")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CONTADORES DIÁRIOS (orçamento de custo, cota do trial)
# ─────────────────────────────────────────────────────────────────────────────

def somar_gasto_diario(escopo: str, dia: str, valor: float) -> float | None:
    """Soma `valor` ao contador (escopo, dia) de forma atômica no banco. Retorna o total ou None."""
    db = _get_client()
    if not db:
        return None
    try:
        res = db.rpc("somar_gasto_diario", {"p_escopo": escopo, "p_dia": dia, "p_valor": valor}).execute()
        return float(res.data) if res.data is not None else None
    except Exception:
        logger.warning("Erro em somar_gasto_diario (migration aplicada?)", exc_info=True)
        return None


def buscar_gasto_diario(escopo: str, dia: str) -> float | None:
    """Total do contador (escopo, dia); 0.0 se ainda não houver linha; None sem banco/erro."""
    db = _get_client()
    if not db:
        return None
    try:
        res = (
            db.table("gastos_diarios")
            .select("total")
            .eq("escopo", escopo)
            .eq("dia", dia)
            .limit(1)
            .execute()
        )
        return float(res.data[0]["total"]) if res.data else 0.0
    except Exception:
        logger.warning("Erro em buscar_gasto_diario (migration aplicada?)", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# LOGS E WEBHOOKS
# ─────────────────────────────────────────────────────────────────────────────

def salvar_log(
    endpoint: str,
    metodo: str,
    status_code: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
    modo: str | None = None,
    tempo_ms: int | None = None,
    cliente_id: str | None = None,
    analise_id: str | None = None,
    erro: str | None = None,
) -> None:
    """Insere log de acesso HTTP (fire-and-forget, ignora erros silenciosamente)."""
    db = _get_client()
    if not db:
        return
    try:
        db.table("logs_acesso").insert({
            "endpoint": endpoint,
            "metodo": metodo,
            "status_code": status_code,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "modo": modo,
            "tempo_resposta_ms": tempo_ms,
            "cliente_id": cliente_id,
            "analise_id": analise_id,
            "erro": erro,
        }).execute()
    except Exception:
        pass  # Logs nunca quebram a aplicação


def salvar_webhook(
    evento: str,
    asaas_payment_id: str,
    status_pagamento: str,
    payload: dict,
) -> None:
    """Persiste evento de webhook recebido do Asaas."""
    db = _get_client()
    if not db:
        return
    try:
        db.table("webhooks_asaas").insert({
            "evento": evento,
            "asaas_payment_id": asaas_payment_id,
            "status_pagamento": status_pagamento,
            "payload": payload,
        }).execute()
    except Exception:
        logger.exception("Erro em salvar_webhook")
