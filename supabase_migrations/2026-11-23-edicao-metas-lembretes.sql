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
