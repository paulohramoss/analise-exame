-- Three Health AI — clientes.premium_expira como fonte única da validade
--
-- Até aqui o app (web/accounts.py) calculava a validade pelo pagamento
-- (confirmação + cookie_max_age do plano) e o banco por conta própria (trigger,
-- em meses). Agora o login lê clientes.premium_expira, a mesma coluna da view
-- clientes_premium_ativos. Para os dois baterem, o trigger usa exatamente as
-- durações de web/config.py PLANS[...]["cookie_max_age"]:
--   mensal 30 dias · semestral 183 dias · anual 365 dias
-- (tests/test_premium_fonte_unica.py falha se um lado mudar sem o outro).
--
-- Comportamento mantido da 2026-11-17-premium-expira-por-plano.sql:
--  * renovação soma a partir do vencimento atual;
--  * REFUNDED / CHARGEBACK_REQUESTED de pagamento confirmado desconta os dias
--    e desliga clientes.premium se a validade zerar;
--  * plano desconhecido conta como mensal (conservador).
--
-- Idempotente.

CREATE OR REPLACE FUNCTION public.atualizar_premium_cliente()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE
    v_duracao interval;
BEGIN
    IF NEW.cliente_id IS NULL THEN
        RETURN NEW;
    END IF;

    v_duracao := CASE lower(coalesce(NEW.plano, ''))
                    WHEN 'mensal'    THEN INTERVAL '30 days'
                    WHEN 'semestral' THEN INTERVAL '183 days'
                    WHEN 'anual'     THEN INTERVAL '365 days'
                    ELSE INTERVAL '30 days'  -- plano desconhecido: conservador
                 END;

    -- Pagamento confirmado: libera/renova
    IF NEW.status IN ('CONFIRMED','RECEIVED')
       AND OLD.status NOT IN ('CONFIRMED','RECEIVED') THEN

        UPDATE public.clientes
        SET premium        = TRUE,
            premium_desde  = COALESCE(premium_desde, NOW()),
            premium_expira = GREATEST(COALESCE(premium_expira, NOW()), NOW()) + v_duracao
        WHERE id = NEW.cliente_id;

        NEW.confirmado_em = NOW();

    -- Pagamento que tinha sido confirmado foi reembolsado / contestado: devolve os dias
    ELSIF NEW.status IN ('REFUNDED','CHARGEBACK_REQUESTED')
          AND OLD.status IN ('CONFIRMED','RECEIVED') THEN

        UPDATE public.clientes
        SET premium_expira = premium_expira - v_duracao,
            premium        = (premium_expira - v_duracao) > NOW()
        WHERE id = NEW.cliente_id;
    END IF;

    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_premium_ao_confirmar ON public.pagamentos;
CREATE TRIGGER trg_premium_ao_confirmar
  BEFORE UPDATE ON public.pagamentos
  FOR EACH ROW EXECUTE FUNCTION public.atualizar_premium_cliente();
