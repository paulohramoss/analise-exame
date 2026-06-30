-- Three Health AI
-- DICOM real, dashboard de clinica e validacao clinica formal.

alter table public.imagens_exame
    add column if not exists origem text default 'upload',
    add column if not exists arquivo_original text,
    add column if not exists dicom_metadata jsonb;

create index if not exists imagens_exame_origem_idx
    on public.imagens_exame (origem);

create index if not exists diagnosticos_validados_analise_created_idx
    on public.diagnosticos_validados (analise_id, created_at desc);

create index if not exists diagnosticos_validados_tipo_created_idx
    on public.diagnosticos_validados (tipo_exame, created_at desc);

comment on column public.imagens_exame.origem is 'Origem da imagem processada: upload comum ou dicom.';
comment on column public.imagens_exame.arquivo_original is 'Nome original do arquivo enviado pelo usuario.';
comment on column public.imagens_exame.dicom_metadata is 'Tags DICOM nao identificaveis e hashes anonimizados para auditoria tecnica.';
