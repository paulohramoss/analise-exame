-- Three Health AI — curadoria dos casos validados usados como exemplo no prompt
--
-- O texto de uma validação clínica é livre e escrito por qualquer cliente
-- premium. Até aqui, todo caso com grau_concordancia >= 80 era injetado no
-- prompt das análises de TODOS os clientes do mesmo tipo de exame — vetor de
-- injeção de instruções no modelo e de vazamento de dados clínicos entre
-- clientes. Agora só entra no prompt o que um admin aprovar explicitamente.
--
-- Idempotente. Casos existentes ficam NÃO aprovados (default false): revise e
-- aprove os que servem de referência com, por exemplo:
--   update public.diagnosticos_validados
--      set aprovado_para_referencia = true
--    where id = '<uuid>';

alter table public.diagnosticos_validados
    add column if not exists aprovado_para_referencia boolean not null default false;

create index if not exists diagnosticos_validados_referencia_idx
    on public.diagnosticos_validados (tipo_exame, created_at desc)
    where aprovado_para_referencia;
