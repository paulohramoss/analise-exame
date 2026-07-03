-- Three Health AI — particionamento mensal de logs_acesso + RLS automático
--
-- Contexto: public.logs_acesso é particionada por mês (RANGE em created_at).
-- As partições, a função criar_particoes_logs() e o job pg_cron que a agenda
-- (dia 1 de cada mês, 03:00) foram criados diretamente no banco (SQL Editor
-- do Supabase) e nunca tinham sido versionados aqui. Esta migration
-- formaliza esse estado e é idempotente: segura para rodar tanto num
-- projeto novo quanto no existente.
--
-- Bug corrigido em 2026-07-03: criar_particoes_logs() criava a partição
-- nova mas não habilitava RLS nem aplicava a policy service_role_all,
-- deixando a partição do mês seguinte temporariamente exposta (leitura e
-- escrita por qualquer client com a chave anon) até alguém notar e corrigir
-- manualmente. Achado via advisory de segurança do Supabase MCP em
-- logs_acesso_2026_09. A versão abaixo já cria a partição com RLS + policy
-- no mesmo passo, então o problema não deve voltar a ocorrer.

create or replace function public.criar_particoes_logs()
 returns void
 language plpgsql
 security definer
 set search_path to ''
as $function$
DECLARE
    mes_alvo    DATE;
    nome_tabela TEXT;
    data_inicio TEXT;
    data_fim    TEXT;
    i           INT;
BEGIN
    FOR i IN 0..2 LOOP
        mes_alvo    := DATE_TRUNC('month', NOW()) + (i || ' months')::INTERVAL;
        nome_tabela := 'logs_acesso_' || TO_CHAR(mes_alvo, 'YYYY_MM');
        data_inicio := TO_CHAR(mes_alvo, 'YYYY-MM-DD');
        data_fim    := TO_CHAR(mes_alvo + INTERVAL '1 month', 'YYYY-MM-DD');

        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = nome_tabela
        ) THEN
            EXECUTE format(
                'CREATE TABLE public.%I PARTITION OF public.logs_acesso FOR VALUES FROM (%L) TO (%L)',
                nome_tabela, data_inicio, data_fim
            );
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', nome_tabela);
            EXECUTE format(
                'CREATE POLICY service_role_all ON public.%I FOR ALL USING (auth.role() = ''service_role'')',
                nome_tabela
            );
            RAISE NOTICE 'Partição criada: %', nome_tabela;
        END IF;
    END LOOP;

    FOR nome_tabela IN
        SELECT c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname ~ '^logs_acesso_\d{4}_\d{2}$'
          AND TO_DATE(SUBSTRING(c.relname FROM '\d{4}_\d{2}$'), 'YYYY_MM')
              < DATE_TRUNC('month', NOW()) - INTERVAL '12 months'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS public.%I', nome_tabela);
        RAISE NOTICE 'Partição removida (>12 meses): %', nome_tabela;
    END LOOP;
END;
$function$;

-- Agenda (ou reagenda, se já existir com esse nome) o job mensal.
select cron.schedule(
    'criar-particoes-logs-mensais',
    '0 3 1 * *',
    $$SELECT criar_particoes_logs()$$
);

-- Rede de segurança: garante RLS + policy em qualquer partição existente
-- que ainda não tenha (cobre o caso já corrigido manualmente em 2026-07 e
-- protege contra regressões futuras se este arquivo for reexecutado).
do $$
declare
    r record;
begin
    for r in
        select c.relname
        from pg_catalog.pg_class c
        join pg_catalog.pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public'
          and c.relname ~ '^logs_acesso(_\d{4}_\d{2}|_default)?$'
          and not c.relrowsecurity
    loop
        execute format('alter table public.%I enable row level security', r.relname);
    end loop;

    for r in
        select c.relname
        from pg_catalog.pg_class c
        join pg_catalog.pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public'
          and c.relname ~ '^logs_acesso(_\d{4}_\d{2}|_default)?$'
          and not exists (
              select 1 from pg_policies p
              where p.schemaname = 'public'
                and p.tablename = c.relname
                and p.policyname = 'service_role_all'
          )
    loop
        execute format(
            'create policy service_role_all on public.%I for all using (auth.role() = ''service_role'')',
            r.relname
        );
    end loop;
end $$;
