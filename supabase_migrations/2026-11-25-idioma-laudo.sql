-- Three Health AI — idioma do laudo
--
-- analises.idioma: 'pt-BR' (padrão) ou 'en'. A análise é feita em português e,
-- quando o profissional escolhe inglês, o laudo pronto é traduzido
-- (core/idioma.py). O idioma define os rótulos do PDF, do painel laboratorial e
-- da explicação para o paciente.
--
-- Idempotente. Enquanto não for aplicada, laudos em português são gravados
-- normalmente e os em inglês são gravados sem o campo (abrem com os rótulos
-- em português).

alter table public.analises
    add column if not exists idioma text not null default 'pt-BR';

alter table public.analises
    drop constraint if exists analises_idioma_check;
alter table public.analises
    add constraint analises_idioma_check check (idioma in ('pt-BR', 'en'));

comment on column public.analises.idioma is
    'Idioma do laudo: pt-BR (padrão) ou en (laudo traduzido no fim da análise).';
