-- Three Health AI — Row Level Security em todas as tabelas da aplicação
--
-- A aplicação acessa o banco só com a chave secreta (service_role), que
-- ignora RLS. Mas o schema public é exposto pela API REST do Supabase: sem
-- RLS, quem obtiver a chave publishable/anon (ela é pública por design)
-- consegue ler e escrever direto nas tabelas — inclusive clientes, com
-- senha_hash e CPF/CNPJ.
--
-- Com RLS ligado e nenhuma policy para anon/authenticated, essas chaves não
-- enxergam nada. A policy service_role_all segue a convenção já usada nas
-- partições de logs_acesso (2026-07-03-particoes-logs-rls-automatica.sql);
-- ela é redundante para a service_role (que já ignora RLS), mas deixa a
-- intenção explícita para o advisor de segurança do Supabase.
--
-- Idempotente. Não altera dados.

do $$
declare
    tabela text;
begin
    foreach tabela in array array[
        'clientes', 'pagamentos', 'sessoes_acesso', 'analises', 'imagens_exame',
        'feedbacks_ia', 'diagnosticos_validados', 'logs_acesso', 'webhooks_asaas', 'gastos_diarios'
    ]
    loop
        if to_regclass('public.' || tabela) is null then
            continue;
        end if;

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

-- Conferência (deve listar todas as tabelas acima com rowsecurity = true):
--   select tablename, rowsecurity from pg_tables where schemaname = 'public' order by 1;
