-- Three Health AI — edição do laudo pelo profissional
--
-- analises.analise_ia: texto gerado pela IA, copiado na primeira edição e nunca
-- mais alterado. analise_completa passa a ser a versão do profissional.
-- analises.edicao_resumo: o que mudou (contagens e títulos de seção, sem texto
-- clínico), para o painel "quanto da IA é confirmado" (core/laudo_edicao.py).
--
-- Idempotente. Enquanto não for aplicada, o laudo abre normalmente e só a
-- edição fica indisponível.

alter table public.analises
    add column if not exists analise_ia      text,
    add column if not exists editado_em      timestamptz,
    add column if not exists edicao_resumo   jsonb;

comment on column public.analises.analise_ia is
    'Versão original da IA, preservada na primeira edição do profissional (analise_completa = versão dele).';

create index if not exists analises_editadas_idx
    on public.analises (cliente_id, editado_em desc) where editado_em is not null;

-- ─────────────────────────────────────────────────────────────────────────────
-- Metas por paciente e lembretes de reavaliação (core/acompanhamento.py)
--
-- metas_paciente: alvo numérico de um exame ("LSI quadríceps ≥ 90", "ferritina
-- > 50") ou meta livre com prazo ("retorno ao esporte"). O progresso é calculado
-- na hora, a partir dos valores já gravados nos laudos do paciente.
-- lembretes: "repetir CK em 2 semanas". O e-mail diário (/cron/lembretes) só
-- diz quantos há — nunca o nome do paciente nem o texto do lembrete.
-- Ambos somem com o paciente e com a conta do profissional (cascata).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.metas_paciente (
    id            uuid primary key default gen_random_uuid(),
    paciente_id   uuid not null references public.pacientes (id) on delete cascade,
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    item          text not null,                 -- exame/medida ("LDL", "LSI quadríceps") ou meta livre
    operador      text check (operador in ('>=', '<=', '>', '<')),
    valor         numeric,
    unidade       text,
    prazo         date,
    concluida_em  timestamptz,                   -- meta livre marcada como cumprida
    created_at    timestamptz not null default now()
);

create index if not exists metas_paciente_idx
    on public.metas_paciente (cliente_id, paciente_id);

create table if not exists public.lembretes (
    id                uuid primary key default gen_random_uuid(),
    cliente_id        uuid not null references public.clientes (id) on delete cascade,
    paciente_id       uuid references public.pacientes (id) on delete cascade,
    texto             text not null,
    data              date not null,
    concluido_em      timestamptz,
    email_enviado_em  timestamptz,
    created_at        timestamptz not null default now()
);

create index if not exists lembretes_pendentes_idx
    on public.lembretes (cliente_id, data) where concluido_em is null;

do $$
declare
    tabela text;
begin
    foreach tabela in array array['metas_paciente', 'lembretes']
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

-- ─────────────────────────────────────────────────────────────────────────────
-- config_seguranca: impressão digital da chave do prontuário, fixada no 1º uso
-- (core/db.estado_chave_fixada). Nunca guarda a chave, só um hash curto dela.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.config_seguranca (
    chave       text primary key,
    valor       text not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

alter table public.config_seguranca enable row level security;
do $$
begin
    if not exists (
        select 1 from pg_policies p
        where p.schemaname = 'public' and p.tablename = 'config_seguranca' and p.policyname = 'service_role_all'
    ) then
        create policy service_role_all on public.config_seguranca for all using (auth.role() = 'service_role');
    end if;
end $$;
