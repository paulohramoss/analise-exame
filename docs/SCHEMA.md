# Schema do banco (Supabase)

Referência de onboarding para o schema do Postgres usado pela aplicação. O
SQL consolidado e executável está em
[`supabase_migrations/schema.sql`](../supabase_migrations/schema.sql) —
idempotente, seguro de rodar tanto em um projeto Supabase novo quanto no
existente.

Toda a persistência é **fire-and-forget**: se `SUPABASE_URL`/
`SUPABASE_SERVICE_KEY` não estiverem configurados, ou se qualquer chamada ao
banco falhar, a aplicação loga o erro e segue funcionando sem persistir
aquele dado (ver `core/db.py`). Nenhuma dessas tabelas é obrigatória para a
análise em si funcionar — elas existem para histórico, cobrança e
aprendizado supervisionado.

A aplicação acessa o banco com a **service_role key**, que ignora Row Level
Security. Não há políticas RLS — todo controle de acesso (quem pode ver o
laudo de quem) é feito na camada Flask, não no banco.

## Diagrama de relacionamento

```
clientes ──1:N── pagamentos ──1:1── sessoes_acesso
   │                                     │
   │                                     │ (opcional)
   └──────────────1:N────────────────────┴──> analises
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        │                     │                     │
                  imagens_exame        feedbacks_ia       diagnosticos_validados
                  (1 análise : N imagens)  (1:N)                  (1:N)

logs_acesso  ──> referencia cliente_id e analise_id (auditoria de requisições)
webhooks_asaas ──> eventos brutos do Asaas, correlacionados por asaas_payment_id
```

## Tabelas

| Tabela | O que guarda | Alimentada por |
|---|---|---|
| `clientes` | Usuário que comprou um plano premium (nome, e-mail, hash de senha) | `core/db.py: upsert_cliente`, fluxo de checkout/login |
| `pagamentos` | Cobrança criada no Asaas (PIX/boleto/cartão), status e payload bruto | `/checkout/pay`, `/webhook/asaas` |
| `sessoes_acesso` | Registro de cada cookie premium emitido após pagamento confirmado | `/api/payment/status` |
| `analises` | Cada laudo gerado — texto completo, modelo usado, tipo de exame, quem é o responsável clínico | `/analyze`, `/trial/analyze`, `/api/analyze` |
| `imagens_exame` | Metadados (não os bytes) de cada imagem enviada numa análise | mesmas 3 rotas acima |
| `feedbacks_ia` | Correção/comentário informal sobre um achado do laudo | formulário de feedback |
| `diagnosticos_validados` | Validação clínica formal por profissional — usada como exemplo de calibração (RAG) no prompt de análises futuras do mesmo tipo de exame | `/validacao-clinica` |
| `logs_acesso` | Uma linha por requisição HTTP (exceto `/static`) — endpoint, status, tempo de resposta | hook `after_request` em `app.py` |
| `webhooks_asaas` | Payload bruto de cada notificação recebida do Asaas | `/webhook/asaas` |

## Coisas não óbvias

- **`external_reference` em `pagamentos`** pode vir em dois formatos:
  `"email"` (legado) ou `"email\|plano"` (atual). Ver
  `buscar_pagamento_confirmado_por_email` em `core/db.py`.
- **`clientes.sessao_versao`** vai dentro do cookie premium (`sv`). A cada
  requisição o app compara as duas; incrementar a coluna (troca de senha,
  estorno, ou manualmente) derruba todas as sessões do cliente.
- **Validade do plano** = data de confirmação do pagamento (`confirmedDate`
  em `payload_asaas`, ou `created_at` da cobrança) + duração do plano. Login
  com plano vencido é recusado e o cookie nunca passa do fim do plano.
- **RLS** está ligado em todas as tabelas (`2026-10-13-rls-tabelas.sql`),
  só com a policy `service_role_all`: a chave anon não acessa nada.
- **`diagnosticos_validados.grau_concordancia`** (0-100) é o campo que
  determina se um caso entra como exemplo de calibração no prompt: só casos
  com `grau_concordancia >= 80` (padrão) **e** `aprovado_para_referencia = true`
  são reaproveitados, via `buscar_caso_validado_referencia`, cacheado em
  memória por 15 minutos. A aprovação é manual (admin, no Supabase): o texto
  é livre e entra no prompt de todos os clientes, então precisa de curadoria
  (sem dados identificáveis, sem instruções ao modelo).
- **`analises` tem colunas opcionais** (`responsavel_*`) adicionadas depois
  da criação inicial da tabela. `salvar_analise`/`listar_analises`/
  `buscar_analise` fazem *fallback* automático (tentam com essas colunas,
  se falhar tentam sem elas) — então mesmo um banco mais antigo sem essas
  colunas não quebra a aplicação. Rodar `schema.sql` remove essa
  necessidade de fallback.
- **Nenhuma tabela guarda os bytes da imagem do exame** — só metadados
  (hash MD5, tamanho, MIME). Isso significa que não é possível reabrir ou
  re-auditar a imagem original a partir do banco hoje.

## Retenção de dados (LGPD)

Prazos aplicados diariamente por `aplicar_retencao_dados()` (pg_cron, migration
`2026-10-13-retencao-dados.sql`):

| Dado | Prazo | Motivo |
|------|-------|--------|
| `analises` do trial (sem conta) | 90 dias | sem titular cadastrado para consultar depois |
| `sessoes_acesso` | 12 meses | auditoria de acesso ao plano |
| `webhooks_asaas` | 12 meses | o status final fica em `pagamentos` |
| `gastos_diarios` | 90 dias | só o dia corrente é usado (e o histórico de custos) |
| `logs_acesso` | 12 meses | partições descartadas por `criar_particoes_logs()`; Marco Civil exige ≥ 6 meses |
| laudos de clientes | enquanto a conta existir | o titular exclui pela página `/conta` |
| `pagamentos` | guarda fiscal | mantidos sem vínculo após a exclusão da conta (LGPD art. 16, I) |

A descrição clínica passa por `core/privacy.py` (remove nome, CPF, CNS, RG,
telefone, e-mail e data de nascimento) antes de ir aos modelos de IA e ao banco.

