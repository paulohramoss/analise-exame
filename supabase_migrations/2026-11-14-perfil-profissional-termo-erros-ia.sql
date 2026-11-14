-- Three Health AI — perfil profissional, termo de responsabilidade e "a IA errou isso"
--
-- 1. clientes: perfil profissional salvo na conta (preenche o responsável do
--    laudo) e aceite do termo de responsabilidade (data + versão aceita).
-- 2. feedbacks_ia: categoria do erro apontado em um clique e a autorização do
--    profissional para o caso entrar no conjunto de avaliação (evals/).
-- 3. Libera a seção 'erro_ia' na constraint feedbacks_ia_secao_check.
--
-- Idempotente. Enquanto não for aplicada, o app continua funcionando: o termo
-- é pedido em toda análise e o erro apontado é gravado como comentário.

alter table public.clientes
    add column if not exists profissional_nome         text,
    add column if not exists profissional_perfil       text,
    add column if not exists profissional_registro     text,
    add column if not exists profissional_instituicao  text,
    add column if not exists termo_aceito_em           timestamptz,
    add column if not exists termo_versao              text;

comment on column public.clientes.profissional_registro is
    'Registro no conselho (CRM, CREFITO…) com formato conferido pelo app; não é consulta ao conselho.';
comment on column public.clientes.termo_versao is
    'Versão do termo de responsabilidade aceita (web/professional.py TERMO_VERSAO).';

alter table public.feedbacks_ia
    add column if not exists categoria_erro      text,
    add column if not exists autoriza_avaliacao  boolean not null default false;

comment on column public.feedbacks_ia.categoria_erro is
    'Erro apontado em um clique (secao = erro_ia): achado_inventado, achado_perdido, alerta_perdido, alerta_indevido, regiao_lado, classificacao.';

create index if not exists feedbacks_ia_erro_idx
    on public.feedbacks_ia (created_at desc) where secao = 'erro_ia';

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
          from unnest(permitidas || array['validacao_medica', 'validacao_clinica_formal', 'erro_ia']) as v
    );

    alter table public.feedbacks_ia drop constraint if exists feedbacks_ia_secao_check;
    execute format(
        'alter table public.feedbacks_ia add constraint feedbacks_ia_secao_check '
        'check (secao is null or secao = any (%L::text[]))',
        permitidas
    );
end $$;
