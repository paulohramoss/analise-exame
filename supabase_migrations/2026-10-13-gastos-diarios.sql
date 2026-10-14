-- Three Health AI — contadores diários persistentes (orçamento de custo e cota do trial)
--
-- Sem Redis, os contadores de gasto diário (GLOBAL/TRIAL/USER_DAILY_COST_BUDGET_USD)
-- viviam na memória de cada instância serverless da Vercel: zeravam a cada cold
-- start e cada instância tinha o seu, então o teto praticamente não existia.
-- Também guarda a contagem de análises do trial por IP (TRIAL_MAX_PER_IP_PER_DAY).
--
-- A aplicação usa Redis quando configurado e cai para esta tabela caso contrário
-- (ver core/cost_tracking.py). Idempotente.

create table if not exists public.gastos_diarios (
    escopo      text        not null,   -- 'global' | 'trial' | 'user:<id>' | 'trial_ip:<hash>'
    dia         date        not null,   -- dia UTC
    total       numeric(14, 6) not null default 0,
    updated_at  timestamptz not null default now(),
    primary key (escopo, dia)
);

alter table public.gastos_diarios enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'gastos_diarios' and policyname = 'service_role_all'
    ) then
        create policy service_role_all on public.gastos_diarios
            for all using (auth.role() = 'service_role');
    end if;
end $$;

-- Incremento atômico (insert ... on conflict): duas instâncias somando ao
-- mesmo tempo não perdem nenhuma parcela.
create or replace function public.somar_gasto_diario(p_escopo text, p_dia date, p_valor numeric)
returns numeric
language sql
security invoker
set search_path = ''
as $$
    insert into public.gastos_diarios as g (escopo, dia, total)
    values (p_escopo, p_dia, p_valor)
    on conflict (escopo, dia)
    do update set total = g.total + excluded.total, updated_at = now()
    returning g.total;
$$;

revoke all on function public.somar_gasto_diario(text, date, numeric) from public, anon, authenticated;

-- Limpeza opcional (os contadores só importam no dia corrente):
--   delete from public.gastos_diarios where dia < current_date - 30;
