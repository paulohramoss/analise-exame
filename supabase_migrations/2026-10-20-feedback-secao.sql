-- Three Health AI — libera as seções usadas pelo feedback médico
--
-- Em produção existe a constraint feedbacks_ia_secao_check (criada fora deste
-- repositório) que recusa as seções gravadas pelo app: 'validacao_medica'
-- (botões "Concordo/Discordo") e 'validacao_clinica_formal' (formulário de
-- validação formal). Resultado: POST /feedback devolvia 503 e o feedback da
-- validação formal se perdia em silêncio.
--
-- Recria a constraint aceitando as seções do app e todas as que já existem na
-- tabela, para não rejeitar nenhum valor antigo. Idempotente.

do $$
declare
    permitidas text[];
begin
    select coalesce(array_agg(distinct secao), '{}')
      into permitidas
      from public.feedbacks_ia
     where secao is not null;

    permitidas := array(
        select distinct v
          from unnest(permitidas || array['validacao_medica', 'validacao_clinica_formal']) as v
    );

    alter table public.feedbacks_ia drop constraint if exists feedbacks_ia_secao_check;
    execute format(
        'alter table public.feedbacks_ia add constraint feedbacks_ia_secao_check '
        'check (secao is null or secao = any (%L::text[]))',
        permitidas
    );
end $$;
