-- Three Health AI — dinamometria e exames laboratoriais
--
-- Além do laudo de imagem, analises.tipo_exame passa a aceitar:
--   'dinamometria'  isocinética, isométrica ou plataforma de força
--   'laboratorial'  sangue e bioquímica
--   'correlacao'    parecer que correlaciona os exames de um paciente (prontuário)
--
-- dados_estruturados guarda os valores lidos do relatório e os calculados pelo
-- sistema (core/dynamometry.py, core/lab_tests.py): déficits, índices de simetria,
-- relações agonista/antagonista, resultados fora da faixa do laudo. Servem para
-- comparar exames do mesmo paciente ao longo do tempo.
--
-- Idempotente. Rode ANTES do deploy: salvar_analise grava a coluna nesses exames.

alter table public.analises
    add column if not exists dados_estruturados jsonb;

comment on column public.analises.dados_estruturados is
    'Valores lidos e calculados de exames estruturados (dinamometria, laboratoriais).';
