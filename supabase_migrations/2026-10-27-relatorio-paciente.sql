-- Three Health AI — relatório do laudo para o paciente, com a marca da clínica
--
-- clientes: nome, contato e logo da clínica, impressos no relatório que o
-- profissional entrega ao paciente. O logo fica como data URL (PNG já
-- reduzido e reprocessado pelo app, ver core/patient_report.processar_logo),
-- sem bucket de storage.
--
-- analises: o texto em linguagem simples gerado pela IA a partir do laudo.
-- Fica gravado para o profissional reabrir e reimprimir sem pagar outra
-- chamada de IA. Não contém nome nem CPF: é gerado só a partir do laudo, e o
-- nome do paciente (do prontuário) entra apenas na página, na hora de exibir.
--
-- Apagados junto com a conta / o laudo, como o resto da linha.
-- Idempotente. Não altera dados existentes.

alter table public.clientes
    add column if not exists clinica_nome text,
    add column if not exists clinica_contato text,
    add column if not exists clinica_logo text;

alter table public.analises
    add column if not exists relatorio_paciente text,
    add column if not exists relatorio_paciente_em timestamptz;
