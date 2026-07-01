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
- **`diagnosticos_validados.grau_concordancia`** (0-100) é o campo que
  determina se um caso entra como exemplo de calibração no prompt: só casos
  com `grau_concordancia >= 80` (padrão) são reaproveitados, via
  `buscar_caso_validado_referencia`, cacheado em memória por 15 minutos.
- **`analises` tem colunas opcionais** (`responsavel_*`) adicionadas depois
  da criação inicial da tabela. `salvar_analise`/`listar_analises`/
  `buscar_analise` fazem *fallback* automático (tentam com essas colunas,
  se falhar tentam sem elas) — então mesmo um banco mais antigo sem essas
  colunas não quebra a aplicação. Rodar `schema.sql` remove essa
  necessidade de fallback.
- **Nenhuma tabela guarda os bytes da imagem do exame** — só metadados
  (hash MD5, tamanho, MIME). Isso significa que não é possível reabrir ou
  re-auditar a imagem original a partir do banco hoje.
