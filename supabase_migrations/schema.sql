-- Three Health AI — schema consolidado (referência de onboarding)
--
-- Este arquivo documenta o schema completo do banco em um único lugar.
-- As migrations anteriores (2026-05-27-*.sql) já foram aplicadas em produção
-- e permanecem como registro histórico — não delete-as. Este arquivo aqui é
-- gerado por engenharia reversa a partir do uso real em core/db.py e serve
-- como referência consolidada, não como uma nova migration a ser "aplicada"
-- por cima das anteriores.
--
-- Idempotente: seguro executar tanto em um projeto Supabase novo (cria tudo
-- do zero) quanto no projeto existente (todo `create table`/`add column` usa
-- `if not exists`, então não há efeito sobre dados já presentes).
--
-- Segurança: a aplicação acessa o banco com a service_role key (ver
-- SUPABASE_SERVICE_KEY no .env.example), que ignora Row Level Security.
-- Não há políticas RLS definidas aqui — todo controle de acesso é feito na
-- camada da aplicação (Flask), não no banco.

create extension if not exists pgcrypto;

-- ─────────────────────────────────────────────────────────────────────────────
-- CLIENTES — usuários que compraram um plano premium
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.clientes (
    id                 uuid primary key default gen_random_uuid(),
    nome               text not null,
    email              text not null unique,
    cpf_cnpj           text,
    asaas_customer_id  text,
    senha_hash         text,
    created_at         timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- PAGAMENTOS — cobranças criadas no Asaas (PIX/boleto/cartão)
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.pagamentos (
    id                   uuid primary key default gen_random_uuid(),
    asaas_payment_id     text not null unique,
    cliente_id           uuid references public.clientes (id) on delete set null,
    valor                numeric(10, 2),
    descricao            text,
    invoice_url          text,
    external_reference   text,
    plano                text,
    forma_pagamento      text,
    status               text,
    payload_asaas        jsonb,
    created_at           timestamptz not null default now()
);

create index if not exists pagamentos_external_reference_idx
    on public.pagamentos (external_reference);

-- ─────────────────────────────────────────────────────────────────────────────
-- SESSÕES DE ACESSO — cookie premium emitido após confirmação de pagamento
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.sessoes_acesso (
    id            uuid primary key default gen_random_uuid(),
    cliente_id    uuid references public.clientes (id) on delete set null,
    pagamento_id  uuid references public.pagamentos (id) on delete set null,
    ip_address    text,
    user_agent    text,
    created_at    timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- ANÁLISES — cada laudo gerado (trial, premium ou API)
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.analises (
    id                       uuid primary key default gen_random_uuid(),
    cliente_id               uuid references public.clientes (id) on delete set null,
    sessao_id                uuid references public.sessoes_acesso (id) on delete set null,
    tipo_exame               text,
    modalidade               text,
    analise_completa         text,
    modelo_ia                text,
    referencias_usadas       integer,
    num_imagens              integer,
    modo                     text,                    -- 'trial' | 'premium' | 'api'
    descricao_usuario        text,
    ip_address               text,
    user_agent               text,
    tempo_processamento_ms   integer,
    responsavel_nome         text,
    responsavel_perfil       text,
    responsavel_registro     text,
    responsavel_instituicao  text,
    created_at               timestamptz not null default now()
);

create index if not exists analises_cliente_created_idx
    on public.analises (cliente_id, created_at desc);

comment on column public.analises.responsavel_nome is 'Nome do medico, laudista ou profissional responsavel informado no cabecalho do laudo.';
comment on column public.analises.responsavel_perfil is 'Perfil profissional do responsavel pelo laudo.';
comment on column public.analises.responsavel_registro is 'Registro profissional do responsavel, quando informado.';
comment on column public.analises.responsavel_instituicao is 'Clinica ou instituicao do responsavel, quando informada.';

-- ─────────────────────────────────────────────────────────────────────────────
-- IMAGENS DE EXAME — metadados das imagens de cada análise (não guarda bytes)
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.imagens_exame (
    id                 uuid primary key default gen_random_uuid(),
    analise_id         uuid references public.analises (id) on delete cascade,
    mime_type          text,
    tamanho_bytes      bigint,
    hash_md5           text,
    ordem              integer default 1,
    origem             text default 'upload',   -- 'upload' | 'dicom'
    arquivo_original   text,
    dicom_metadata     jsonb,
    created_at         timestamptz not null default now()
);

create index if not exists imagens_exame_origem_idx
    on public.imagens_exame (origem);

create index if not exists imagens_exame_analise_idx
    on public.imagens_exame (analise_id);

comment on column public.imagens_exame.origem is 'Origem da imagem processada: upload comum ou dicom.';
comment on column public.imagens_exame.arquivo_original is 'Nome original do arquivo enviado pelo usuario.';
comment on column public.imagens_exame.dicom_metadata is 'Tags DICOM nao identificaveis e hashes anonimizados para auditoria tecnica.';

-- ─────────────────────────────────────────────────────────────────────────────
-- FEEDBACKS DA IA — correções/comentários informais sobre um laudo
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.feedbacks_ia (
    id                 uuid primary key default gen_random_uuid(),
    analise_id         uuid references public.analises (id) on delete cascade,
    tipo               text,   -- 'correcao' | 'validacao' | 'comentario' | 'falso_positivo' | 'falso_negativo'
    secao              text,
    achado_original    text,
    achado_corrigido   text,
    comentario         text,
    fonte              text,   -- 'paciente' | 'medico' | 'radiologista' | 'sistema' | 'admin'
    fonte_nome         text,
    created_at         timestamptz not null default now()
);

create index if not exists feedbacks_ia_analise_created_idx
    on public.feedbacks_ia (analise_id, created_at desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- DIAGNÓSTICOS VALIDADOS — validação clínica formal por profissional,
-- usada como exemplo de calibração (RAG) no prompt de análises futuras.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.diagnosticos_validados (
    id                    uuid primary key default gen_random_uuid(),
    analise_id            uuid references public.analises (id) on delete cascade,
    tipo_exame            text,
    modalidade            text,
    diagnostico_ia        text,
    diagnostico_final     text,
    concordancia          boolean,
    grau_concordancia     integer,   -- 0-100 (% de concordância com a IA)
    achados_corretos      jsonb default '[]'::jsonb,
    achados_perdidos      jsonb default '[]'::jsonb,
    achados_incorretos    jsonb default '[]'::jsonb,
    validado_por          text,
    created_at            timestamptz not null default now()
);

create index if not exists diagnosticos_validados_analise_created_idx
    on public.diagnosticos_validados (analise_id, created_at desc);

create index if not exists diagnosticos_validados_tipo_created_idx
    on public.diagnosticos_validados (tipo_exame, created_at desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- LOGS DE ACESSO — uma linha por requisição HTTP (exceto /static)
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.logs_acesso (
    id                   uuid primary key default gen_random_uuid(),
    cliente_id           uuid references public.clientes (id) on delete set null,
    analise_id           uuid references public.analises (id) on delete set null,
    endpoint             text,
    metodo               text,
    status_code          integer,
    ip_address           text,
    user_agent           text,
    modo                 text,
    tempo_resposta_ms    integer,
    erro                 text,
    created_at           timestamptz not null default now()
);

create index if not exists logs_acesso_created_idx
    on public.logs_acesso (created_at desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- WEBHOOKS ASAAS — payload bruto de cada notificação recebida
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.webhooks_asaas (
    id                   uuid primary key default gen_random_uuid(),
    evento               text,
    asaas_payment_id     text,
    status_pagamento     text,
    payload              jsonb,
    created_at           timestamptz not null default now()
);

create index if not exists webhooks_asaas_payment_idx
    on public.webhooks_asaas (asaas_payment_id);
