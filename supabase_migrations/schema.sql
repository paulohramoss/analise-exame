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
-- SUPABASE_SERVICE_KEY no .env.example), que ignora Row Level Security. O
-- controle de acesso por cliente é feito na camada da aplicação (Flask); o
-- RLS (2026-10-13-rls-tabelas.sql) existe para que a chave anon/publishable,
-- pública por design, não leia nem escreva nada pela API REST do Supabase.

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
    sessao_versao      integer not null default 0,   -- incrementar invalida os cookies premium do cliente
    created_at         timestamptz not null default now()
);

alter table public.clientes
    add column if not exists sessao_versao integer not null default 0;

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
    aprovado_para_referencia boolean not null default false,  -- curadoria manual (admin) antes de virar exemplo no prompt
    created_at            timestamptz not null default now()
);

alter table public.diagnosticos_validados
    add column if not exists aprovado_para_referencia boolean not null default false;

create index if not exists diagnosticos_validados_analise_created_idx
    on public.diagnosticos_validados (analise_id, created_at desc);

create index if not exists diagnosticos_validados_tipo_created_idx
    on public.diagnosticos_validados (tipo_exame, created_at desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- LOGS DE ACESSO — uma linha por requisição HTTP (exceto /static)
--
-- Em produção esta tabela é particionada por mês (RANGE em created_at), com
-- partições criadas automaticamente por um job pg_cron. RLS + a policy
-- service_role_all são aplicadas em cada partição nova. Ver
-- 2026-07-03-particoes-logs-rls-automatica.sql para a função, o job e a
-- policy — o create table abaixo é só a definição de colunas de referência,
-- não recria o particionamento.
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

-- ─────────────────────────────────────────────────────────────────────────────
-- PRONTUÁRIO — pacientes, tratamentos, evolução e auditoria de acesso
-- (2026-10-22-prontuario-pacientes.sql). Nome e CPF chegam cifrados pela aplicação.
-- ─────────────────────────────────────────────────────────────────────────────
-- Quando o profissional baixou a cópia dos dados (/conta/dados.json). A tela de
-- exclusão da conta avisa se algum prontuário mudou depois disso.
alter table public.clientes
    add column if not exists dados_exportados_em timestamptz;

create table if not exists public.pacientes (
    id                uuid primary key default gen_random_uuid(),
    cliente_id        uuid not null references public.clientes (id) on delete cascade,
    nome_cifrado      text not null,
    cpf_cifrado       text not null,
    cpf_hash          text not null,
    data_nascimento   date,
    sexo              text,
    observacoes       text,                    -- comorbidades, lado dominante, alergias…
    consentimento_em  timestamptz not null default now(),
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    unique (cliente_id, cpf_hash)
);

create index if not exists pacientes_cliente_idx
    on public.pacientes (cliente_id, created_at desc);

alter table public.analises
    add column if not exists paciente_id uuid references public.pacientes (id) on delete set null;

create index if not exists analises_paciente_created_idx
    on public.analises (paciente_id, created_at)
    where paciente_id is not null;

create table if not exists public.tratamentos (
    id            uuid primary key default gen_random_uuid(),
    paciente_id   uuid not null references public.pacientes (id) on delete cascade,
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    tipo          text not null,               -- fisioterapia, infiltração, cirurgia, medicação…
    descricao     text,
    regiao        text,
    data_inicio   date not null,
    data_fim      date,                        -- null = em andamento
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

alter table public.tratamentos
    add column if not exists updated_at timestamptz not null default now();

create index if not exists tratamentos_paciente_idx
    on public.tratamentos (paciente_id, data_inicio);

create table if not exists public.evolucoes (
    id            uuid primary key default gen_random_uuid(),
    paciente_id   uuid not null references public.pacientes (id) on delete cascade,
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    data          date not null,
    dor_eva       smallint check (dor_eva between 0 and 10),
    funcao        smallint check (funcao between 0 and 100),   -- escore funcional, maior = melhor
    nota          text,
    created_at    timestamptz not null default now()
);

create index if not exists evolucoes_paciente_idx
    on public.evolucoes (paciente_id, data);

-- Trilha de auditoria: quem abriu/alterou qual prontuário. Sobrevive à exclusão
-- do paciente (paciente_id vira null); some com a conta do profissional.
create table if not exists public.acessos_prontuario (
    id            uuid primary key default gen_random_uuid(),
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    paciente_id   uuid references public.pacientes (id) on delete set null,
    acao          text not null,
    created_at    timestamptz not null default now()
);

create index if not exists acessos_prontuario_paciente_idx
    on public.acessos_prontuario (paciente_id, created_at desc);

-- RLS como nas demais tabelas (2026-10-13-rls-tabelas.sql): a chave anon não
-- enxerga nada; a aplicação usa a service_role.
do $$
declare
    tabela text;
begin
    foreach tabela in array array['pacientes', 'tratamentos', 'evolucoes', 'acessos_prontuario']
    loop
        execute format('alter table public.%I enable row level security', tabela);
        if not exists (
            select 1 from pg_policies p
            where p.schemaname = 'public' and p.tablename = tabela and p.policyname = 'service_role_all'
        ) then
            execute format(
                'create policy service_role_all on public.%I for all using (auth.role() = ''service_role'')',
                tabela
            );
        end if;
    end loop;
end $$;
