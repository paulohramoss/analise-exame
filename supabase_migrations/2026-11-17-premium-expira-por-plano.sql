-- Three Health AI — validade do premium por plano e revogação no reembolso
--
-- O trigger trg_premium_ao_confirmar (BEFORE UPDATE em pagamentos) gravava
-- clientes.premium_expira = NOW() + 1 ano para QUALQUER plano. O acesso no app
-- não dependia disso (find_active_plan calcula pela data do pagamento + plano),
-- mas a coluna e a view clientes_premium_ativos ficavam erradas.
--
-- 1. Duração por plano: mensal 1 mês, semestral 6 meses, anual 1 ano
--    (plano desconhecido → 1 mês, conservador).
-- 2. Renovação soma a partir do vencimento atual, sem perder dias já pagos.
-- 3. REFUNDED / CHARGEBACK_REQUESTED de um pagamento confirmado devolve os dias
--    e desliga clientes.premium se a validade zerar.
--
-- Idempotente. Já aplicada em produção em 2026-11-17.

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
                    WHEN 'mensal'    THEN INTERVAL '1 month'
                    WHEN 'semestral' THEN INTERVAL '6 months'
                    WHEN 'anual'     THEN INTERVAL '1 year'
                    ELSE INTERVAL '1 month'  -- plano desconhecido: conservador
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

-- O trigger já existe (BEFORE UPDATE ON public.pagamentos); recriado aqui por segurança
DROP TRIGGER IF EXISTS trg_premium_ao_confirmar ON public.pagamentos;
CREATE TRIGGER trg_premium_ao_confirmar
  BEFORE UPDATE ON public.pagamentos
  FOR EACH ROW EXECUTE FUNCTION public.atualizar_premium_cliente();
