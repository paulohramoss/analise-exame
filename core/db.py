"""
Integração com Supabase — Three Health Platform.

Todas as operações são fire-and-forget: erros são logados mas nunca propagados
para não bloquear o fluxo principal da aplicação.
"""

import importlib
import logging
import os
import time
from functools import lru_cache
from collections import Counter

logger = logging.getLogger(__name__)

# Cache em memória do melhor caso validado por tipo de exame, usado como
# exemplo de calibração no prompt de análise. TTL curto para não bater no
# Supabase a cada análise, mas ainda refletir novas validações em minutos.
_validated_case_cache: dict = {}
_VALIDATED_CASE_CACHE_TTL_SECONDS = 900


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


def buscar_pagamento_confirmado_por_email(email: str) -> tuple[str, str] | None:
    """Retorna (asaas_payment_id, external_reference) mais recente confirmado para o e-mail, ou None.
    Suporta external_reference no formato 'email' (legado) ou 'email|plano' (atual).
    """
    db = _get_client()
    if not db or not email:
        return None
    try:
        res = (
            db.table("pagamentos")
            .select("asaas_payment_id, external_reference")
            .ilike("external_reference", f"{email}%")
            .in_("status", ["CONFIRMED", "RECEIVED"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            return row["asaas_payment_id"], row.get("external_reference", "")
        return None
    except Exception:
        logger.exception("Erro em buscar_pagamento_confirmado_por_email")
        return None


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
) -> str | None:
    """Persiste resultado de análise da IA. Retorna UUID da análise ou None."""
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
        optional_keys: list[str] = []
        if responsavel:
            optional_fields = {
                "responsavel_nome": responsavel.get("name") or None,
                "responsavel_perfil": responsavel.get("role") or None,
                "responsavel_registro": responsavel.get("register") or None,
                "responsavel_instituicao": responsavel.get("organization") or None,
            }
            data.update(optional_fields)
            optional_keys = list(optional_fields.keys())

        try:
            res = db.table("analises").insert(data).execute()
        except Exception as e:
            if not optional_keys:
                raise e
            for key in optional_keys:
                data.pop(key, None)
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

    base_cols = (
        "id, created_at, tipo_exame, modelo_ia, referencias_usadas, num_imagens, "
        "modo, descricao_usuario, modalidade, tempo_processamento_ms"
    )
    optional_cols = (
        ", responsavel_nome, responsavel_perfil, responsavel_registro, "
        "responsavel_instituicao"
    )

    def _run_query(columns: str):
        query = db.table("analises").select(columns)
        if cliente_id:
            query = query.eq("cliente_id", cliente_id)
        query = query.order("created_at", desc=True).limit(limit)
        return query.execute()

    try:
        res = _run_query(base_cols + optional_cols)
        return res.data or []
    except Exception:
        try:
            res = _run_query(base_cols)
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

    base_cols = (
        "id, created_at, tipo_exame, analise_completa, modelo_ia, referencias_usadas, "
        "num_imagens, modo, descricao_usuario, modalidade, tempo_processamento_ms, cliente_id"
    )
    optional_cols = (
        ", responsavel_nome, responsavel_perfil, responsavel_registro, "
        "responsavel_instituicao"
    )

    def _run_query(columns: str):
        query = db.table("analises").select(columns).eq("id", analise_id)
        if cliente_id:
            query = query.eq("cliente_id", cliente_id)
        return query.limit(1).execute()

    try:
        res = _run_query(base_cols + optional_cols)
        return res.data[0] if res.data else None
    except Exception:
        try:
            res = _run_query(base_cols)
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
        data = {
            "analise_id": analise_id,
            "mime_type": mime_type,
            "tamanho_bytes": tamanho_bytes,
            "hash_md5": hash_md5,
            "ordem": ordem,
        }
        optional_fields = {
            "origem": origem or "upload",
            "arquivo_original": arquivo_original or None,
            "dicom_metadata": dicom_metadata or None,
        }
        data.update(optional_fields)
        try:
            db.table("imagens_exame").insert(data).execute()
        except Exception:
            for key in optional_fields:
                data.pop(key, None)
            db.table("imagens_exame").insert(data).execute()
    except Exception:
        logger.exception("Erro em salvar_imagem_exame")


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
    grau_concordancia >= min_concordancia, para uso como exemplo de
    calibração no prompt de análise. Cacheado em memória por
    _VALIDATED_CASE_CACHE_TTL_SECONDS (inclusive resultados vazios).
    """
    now = time.time()
    cached = _validated_case_cache.get(tipo_exame)
    if cached and cached["expires_at"] > now:
        return cached["case"]

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
                .gte("grau_concordancia", min_concordancia)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                case = res.data[0]
        except Exception:
            logger.exception("Erro em buscar_caso_validado_referencia")

    _validated_case_cache[tipo_exame] = {
        "case": case,
        "expires_at": now + _VALIDATED_CASE_CACHE_TTL_SECONDS,
    }
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
