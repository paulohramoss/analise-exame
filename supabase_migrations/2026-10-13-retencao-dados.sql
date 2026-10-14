-- Three Health AI — política de retenção de dados (LGPD art. 15 e 16)
--
-- Dado pessoal só fica guardado enquanto tem finalidade. Prazos:
--
--   analises (modo 'trial', sem conta)   90 dias  — não há titular cadastrado para
--                                                   consultar o histórico depois
--   sessoes_acesso                       12 meses — auditoria de acesso ao plano
--   webhooks_asaas (payload bruto)       12 meses — o status final fica em pagamentos
--   gastos_diarios (contadores)          90 dias  — só o dia corrente importa
--   logs_acesso                          12 meses — já existente: partições antigas
--                                                   são descartadas por
--                                                   criar_particoes_logs() (Marco Civil,
--                                                   art. 15, exige ao menos 6 meses)
--   analises de clientes, pagamentos     enquanto a conta existir; a exclusão pelo
--                                         titular (/conta) apaga os laudos e mantém só
--                                         as cobranças (guarda fiscal, art. 16, I)
--
-- Idempotente. Agenda a limpeza diária às 04:00 (UTC) via pg_cron.

create or replace function public.aplicar_retencao_dados()
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    delete from public.analises
     where modo = 'trial' and cliente_id is null
       and created_at < now() - interval '90 days';

    delete from public.sessoes_acesso
     where created_at < now() - interval '12 months';

    delete from public.webhooks_asaas
     where created_at < now() - interval '12 months';

    if to_regclass('public.gastos_diarios') is not null then
        delete from public.gastos_diarios where dia < current_date - 90;
    end if;
end;
$$;

revoke all on function public.aplicar_retencao_dados() from public, anon, authenticated;

select cron.schedule(
    'aplicar-retencao-dados-diaria',
    '0 4 * * *',
    $$SELECT public.aplicar_retencao_dados()$$
);
