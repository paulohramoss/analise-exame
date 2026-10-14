-- Three Health AI — revogação de sessões premium
--
-- O cookie premium é stateless (assinado, vale até o fim do plano). Até aqui
-- não havia como invalidá-lo: troca de senha ou estorno deixavam os cookies
-- já emitidos funcionando. Agora o cookie carrega a versão de sessão do
-- cliente ("sv"); a aplicação compara com esta coluna a cada requisição e
-- incrementá-la derruba todos os cookies do cliente de uma vez.
--
-- Incrementada automaticamente pela aplicação em:
--   - /login/definir-senha (troca de senha)
--   - webhook do Asaas com estorno/chargeback/exclusão da cobrança
-- Para forçar logout manual de um cliente:
--   update public.clientes set sessao_versao = sessao_versao + 1 where email = '<email>';
--
-- Idempotente. Cookies emitidos antes desta migration valem como versão 0.

alter table public.clientes
    add column if not exists sessao_versao integer not null default 0;
