-- Three Health AI — prontuário: pacientes, tratamentos e evolução
--
-- Cada profissional (clientes) cadastra os próprios pacientes e só enxerga os
-- seus: toda tabela daqui carrega cliente_id e a aplicação filtra por ele.
--
-- Nome e CPF são dado pessoal sensível junto com o histórico clínico (LGPD
-- art. 11). Ficam cifrados pela aplicação (core/patient_crypto.py, chave em
-- PRONTUARIO_ENCRYPTION_KEY); o banco só vê texto cifrado. cpf_hash é um HMAC
-- do CPF, usado para impedir o mesmo CPF duas vezes para o mesmo profissional.
--
-- Os laudos ganham paciente_id opcional: laudos avulsos e o trial seguem como
-- antes. Excluir o paciente apaga tratamentos e evoluções e deixa os laudos
-- sem vínculo (on delete set null).
--
-- Idempotente. Não altera dados existentes.

-- Quando o profissional baixou a cópia dos dados (/conta/dados.json). A tela de
-- exclusão da conta avisa se algum prontuário mudou depois disso.
alter table public.clientes
    add column if not exists dados_exportados_em timestamptz;

create table if not exists public.pacientes (
    id                uuid primary key default gen_random_uuid(),
    cliente_id        uuid not null references public.clientes (id) on delete cascade,
    nome_cifrado      text not null,
    cpf_cifrado       text not null,
    cpf_hash          text not null,
    data_nascimento   date,
    sexo              text,
    observacoes       text,                    -- comorbidades, lado dominante, alergias…
    consentimento_em  timestamptz not null default now(),
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    unique (cliente_id, cpf_hash)
);

create index if not exists pacientes_cliente_idx
    on public.pacientes (cliente_id, created_at desc);

alter table public.analises
    add column if not exists paciente_id uuid references public.pacientes (id) on delete set null;

create index if not exists analises_paciente_created_idx
    on public.analises (paciente_id, created_at)
    where paciente_id is not null;

create table if not exists public.tratamentos (
    id            uuid primary key default gen_random_uuid(),
    paciente_id   uuid not null references public.pacientes (id) on delete cascade,
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    tipo          text not null,               -- fisioterapia, infiltração, cirurgia, medicação…
    descricao     text,
    regiao        text,
    data_inicio   date not null,
    data_fim      date,                        -- null = em andamento
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

alter table public.tratamentos
    add column if not exists updated_at timestamptz not null default now();

create index if not exists tratamentos_paciente_idx
    on public.tratamentos (paciente_id, data_inicio);

create table if not exists public.evolucoes (
    id            uuid primary key default gen_random_uuid(),
    paciente_id   uuid not null references public.pacientes (id) on delete cascade,
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    data          date not null,
    dor_eva       smallint check (dor_eva between 0 and 10),
    funcao        smallint check (funcao between 0 and 100),   -- escore funcional, maior = melhor
    nota          text,
    created_at    timestamptz not null default now()
);

create index if not exists evolucoes_paciente_idx
    on public.evolucoes (paciente_id, data);

-- Trilha de auditoria: quem abriu/alterou qual prontuário. Sobrevive à exclusão
-- do paciente (paciente_id vira null); some com a conta do profissional.
create table if not exists public.acessos_prontuario (
    id            uuid primary key default gen_random_uuid(),
    cliente_id    uuid not null references public.clientes (id) on delete cascade,
    paciente_id   uuid references public.pacientes (id) on delete set null,
    acao          text not null,
    created_at    timestamptz not null default now()
);

create index if not exists acessos_prontuario_paciente_idx
    on public.acessos_prontuario (paciente_id, created_at desc);

-- RLS como nas demais tabelas (2026-10-13-rls-tabelas.sql): a chave anon não
-- enxerga nada; a aplicação usa a service_role.
do $$
declare
    tabela text;
begin
    foreach tabela in array array['pacientes', 'tratamentos', 'evolucoes', 'acessos_prontuario']
    loop
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
