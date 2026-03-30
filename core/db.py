"""
Integração com Supabase — Three Health Platform.

Todas as operações são fire-and-forget: erros são logados mas nunca propagados
para não bloquear o fluxo principal da aplicação.
"""

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _get_client():
    """Retorna cliente Supabase (singleton). None se não configurado."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("[DB] SUPABASE_URL ou SUPABASE_SERVICE_KEY não configurados — persistência desativada.")
        return None
    try:
        from supabase import create_client
        client = create_client(url, key)
        print("[DB] Conectado ao Supabase.")
        return client
    except Exception as e:
        print(f"[DB] Falha ao criar cliente Supabase: {e}")
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
    except Exception as e:
        print(f"[DB] upsert_cliente: {e}")
        return None


def buscar_cliente_id_por_email(email: str) -> str | None:
    """Retorna UUID do cliente pelo e-mail, ou None se não encontrado."""
    db = _get_client()
    if not db or not email:
        return None
    try:
        res = db.table("clientes").select("id").eq("email", email).limit(1).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        print(f"[DB] buscar_cliente_id_por_email: {e}")
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
        }
        res = db.table("pagamentos").upsert(data, on_conflict="asaas_payment_id").execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        print(f"[DB] salvar_pagamento: {e}")
        return None


def buscar_pagamento_id(asaas_payment_id: str) -> str | None:
    """Retorna UUID interno do pagamento pelo ID do Asaas, ou None."""
    db = _get_client()
    if not db or not asaas_payment_id:
        return None
    try:
        res = db.table("pagamentos").select("id").eq("asaas_payment_id", asaas_payment_id).limit(1).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        print(f"[DB] buscar_pagamento_id: {e}")
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
    except Exception as e:
        print(f"[DB] atualizar_status_pagamento: {e}")


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
    except Exception as e:
        print(f"[DB] salvar_sessao: {e}")
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
) -> str | None:
    """Persiste resultado de análise da IA. Retorna UUID da análise ou None."""
    db = _get_client()
    if not db:
        return None
    try:
        res = db.table("analises").insert({
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
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        print(f"[DB] salvar_analise: {e}")
        return None


def salvar_imagem_exame(
    analise_id: str,
    mime_type: str,
    tamanho_bytes: int,
    hash_md5: str,
    ordem: int = 1,
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
        }).execute()
    except Exception as e:
        print(f"[DB] salvar_imagem_exame: {e}")


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
    except Exception as e:
        print(f"[DB] salvar_feedback: {e}")
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
    except Exception as e:
        print(f"[DB] salvar_diagnostico_validado: {e}")
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
    except Exception as e:
        print(f"[DB] salvar_webhook: {e}")
