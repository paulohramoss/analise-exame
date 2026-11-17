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
-- Robustez (review do PR #46):
--  * transições NULL-safe: pagamento criado com status NULL também libera ao
--    confirmar (pagamentos.status passa a ter default 'PENDING', como em prod);
--  * revogação reconhece toda a família de estorno/chargeback do Asaas e
--    desconta UMA vez, mesmo passando por estados intermediários
--    (ex.: CONFIRMED → REFUND_IN_PROGRESS → REFUNDED). "Já foi creditado" é
--    confirmado_em preenchido, gravado pelo próprio trigger na confirmação;
--  * chargeback revertido (volta a RECEIVED/CONFIRMED) devolve os dias;
--  * pagamentos_status_check passa a aceitar RECEIVED_IN_CASH,
--    REFUND_IN_PROGRESS e PARTIALLY_REFUNDED: o Asaas manda esses status e o
--    UPDATE do webhook falhava (pagamento em dinheiro nunca liberava acesso).
--
-- Idempotente.

ALTER TABLE public.pagamentos ALTER COLUMN status SET DEFAULT 'PENDING';

ALTER TABLE public.pagamentos DROP CONSTRAINT IF EXISTS pagamentos_status_check;
ALTER TABLE public.pagamentos ADD CONSTRAINT pagamentos_status_check CHECK (status = ANY (ARRAY[
    'PENDING','AWAITING_RISK_ANALYSIS','APPROVED_BY_RISK_ANALYSIS',
    'CONFIRMED','RECEIVED','RECEIVED_IN_CASH',
    'REFUNDED','PARTIALLY_REFUNDED','REFUND_REQUESTED','REFUND_IN_PROGRESS',
    'OVERDUE','CHARGEBACK_DISPUTE','CHARGEBACK_REQUESTED','AWAITING_CHARGEBACK_REVERSAL',
    'DUNNING_REQUESTED','DUNNING_RECEIVED'
]));

CREATE OR REPLACE FUNCTION public.atualizar_premium_cliente()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE
    v_duracao   interval;
    v_pagos     text[] := ARRAY['CONFIRMED','RECEIVED','RECEIVED_IN_CASH'];
    v_revogados text[] := ARRAY['REFUNDED','PARTIALLY_REFUNDED','REFUND_REQUESTED',
                                'REFUND_IN_PROGRESS','CHARGEBACK_REQUESTED',
                                'CHARGEBACK_DISPUTE','AWAITING_CHARGEBACK_REVERSAL'];
    v_old       text := coalesce(OLD.status, '');
    v_new       text := coalesce(NEW.status, '');
BEGIN
    IF NEW.cliente_id IS NULL OR v_new = v_old THEN
        RETURN NEW;
    END IF;

    v_duracao := CASE lower(coalesce(NEW.plano, ''))
                    WHEN 'mensal'    THEN INTERVAL '30 days'
                    WHEN 'semestral' THEN INTERVAL '183 days'
                    WHEN 'anual'     THEN INTERVAL '365 days'
                    ELSE INTERVAL '30 days'  -- plano desconhecido: conservador
                 END;

    -- Entrou em "pago" vindo de qualquer outro estado (inclusive NULL ou de um
    -- chargeback revertido): libera/renova, somando a partir do vencimento atual.
    IF v_new = ANY (v_pagos) AND NOT (v_old = ANY (v_pagos)) THEN

        UPDATE public.clientes
        SET premium        = TRUE,
            premium_desde  = COALESCE(premium_desde, NOW()),
            premium_expira = GREATEST(COALESCE(premium_expira, NOW()), NOW()) + v_duracao
        WHERE id = NEW.cliente_id;

        NEW.confirmado_em = NOW();

    -- Entrou na família de estorno/chargeback vindo de fora dela, e o pagamento
    -- já tinha sido creditado: desconta uma vez só.
    ELSIF v_new = ANY (v_revogados) AND NOT (v_old = ANY (v_revogados))
          AND (v_old = ANY (v_pagos) OR NEW.confirmado_em IS NOT NULL) THEN

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
