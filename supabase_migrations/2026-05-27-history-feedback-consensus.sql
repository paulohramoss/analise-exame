-- Three Health AI
-- Historico, PDF profissional, feedback medico e consenso visivel.
-- Execute no SQL Editor do Supabase para persistir os dados do responsavel.

alter table public.analises
    add column if not exists responsavel_nome text,
    add column if not exists responsavel_perfil text,
    add column if not exists responsavel_registro text,
    add column if not exists responsavel_instituicao text;

create index if not exists analises_cliente_created_idx
    on public.analises (cliente_id, created_at desc);

create index if not exists feedbacks_ia_analise_created_idx
    on public.feedbacks_ia (analise_id, created_at desc);

comment on column public.analises.responsavel_nome is 'Nome do medico, laudista ou profissional responsavel informado no cabecalho do laudo.';
comment on column public.analises.responsavel_perfil is 'Perfil profissional do responsavel pelo laudo.';
comment on column public.analises.responsavel_registro is 'Registro profissional do responsavel, quando informado.';
comment on column public.analises.responsavel_instituicao is 'Clinica ou instituicao do responsavel, quando informada.';
