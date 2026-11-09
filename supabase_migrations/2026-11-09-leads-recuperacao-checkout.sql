-- Three Health AI — leads do teste grátis e recuperação de checkout abandonado
--
-- leads: e-mails de quem usou o /trial e pediu para receber artigos e
-- novidades. O teste grátis é anônimo; o lead só existe com consentimento
-- explícito (checkbox desmarcado por padrão). consentimento_texto guarda a
-- frase exata que a pessoa aceitou, como prova do opt-in (LGPD art. 8º).
--
-- email_descadastros: lista de supressão única. Quem clica em "descadastrar"
-- (em qualquer e-mail de marketing ou lembrete) entra aqui e não recebe mais
-- nada que não seja transacional. Um novo opt-in explícito tira o e-mail da
-- lista. O descadastro também apaga a linha de leads (minimização).
--
-- pagamentos.lembrete_enviado_em: marca a cobrança que já recebeu o lembrete
-- de checkout abandonado, para nunca mandar o mesmo lembrete duas vezes.
--
-- Idempotente. Não altera dados existentes.

create table if not exists public.leads (
    id                   uuid primary key default gen_random_uuid(),
    email                text not null unique,
    origem               text not null default 'trial',
    consentimento_texto  text not null,
    consentimento_em     timestamptz not null default now(),
    created_at           timestamptz not null default now()
);

create table if not exists public.email_descadastros (
    email       text primary key,
    origem      text,
    created_at  timestamptz not null default now()
);

alter table public.pagamentos
    add column if not exists lembrete_enviado_em timestamptz;

-- Busca diária de cobranças pendentes ainda sem lembrete.
create index if not exists pagamentos_sem_lembrete_idx
    on public.pagamentos (created_at)
    where lembrete_enviado_em is null;

-- RLS: mesma convenção de 2026-10-13-rls-tabelas.sql (só a service_role acessa).
do $$
declare
    tabela text;
begin
    foreach tabela in array array['leads', 'email_descadastros']
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
