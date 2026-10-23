# MedAI Analyzer

> Analisador de exames de imagem musculoesqueléticos, com laudo estruturado gerado por consenso entre Google Gemini e Claude.

---

## Visão Geral

O **MedAI Analyzer** (plataforma Three Health) analisa imagens de exames ortopédicos — RM, Raio-X, TC e US de joelho, coluna, ombro, quadril, pé/tornozelo, mão/punho e cotovelo. O sistema identifica a região anatômica, compara com imagens de referência normais e gera um laudo estruturado com achados classificados por força de evidência, impressão diagnóstica e conduta. Com a chave da Anthropic configurada, Gemini e Claude analisam o exame de forma independente e uma síntese de consenso produz o laudo final.

> **Aviso clínico:** Esta ferramenta destina-se exclusivamente ao uso como auxiliar por profissionais de saúde habilitados. **Não substitui** o diagnóstico médico especializado.

---

## Funcionalidades

- Upload de PNG, JPG, WEBP, GIF e **DICOM** (`.dcm`, convertido e com metadados anonimizados)
- Detecção da região anatômica pela descrição/nome do arquivo e, se preciso, pela própria imagem
- Comparação com imagens de referência normais (`reference_data/<região>/`)
- Laudo por consenso dual Gemini + Claude (ou só Gemini, sem chave Anthropic)
- Achados circulados dentro das imagens por grau de certeza (verde = forte, amarelo = moderado, vermelho = baixo), com a localização exata de cada estrutura alterada, na tela e no PDF
- Planos pagos (Asaas: PIX, boleto, cartão na página do Asaas), trial gratuito embutível por iframe
- Histórico de laudos, dashboard da clínica e validação clínica dos laudos
- Prontuário de pacientes: cadastro com nome e CPF (cifrados), laudos vinculados, tratamentos, evolução (dor e função) com gráfico e linha do tempo. Cada profissional vê só os próprios pacientes
- API REST autenticada por chave (`/api/analyze`)
- Fila assíncrona opcional (Redis + RQ) e orçamento de custo diário por escopo
- LGPD: identificadores removidos do texto clínico, exportação e exclusão de conta pelo titular, retenção automática
- Painel interno de custos e qualidade (`/admin/custos`) e avaliação de laudos (`evals/`)

---

## Estrutura do Projeto

```
analise-exame/
├── app.py                  # Monta a aplicação Flask (create_app); objeto WSGI `app`
├── worker.py               # Worker da fila de análise (RQ) — processo separado
├── api/index.py            # Entrada da Vercel (reexporta `app`)
├── web/                    # Camada HTTP
│   ├── routes/             # Blueprints: analysis, laudos, billing, auth
│   ├── security.py         # Cookies/tokens assinados, sessão premium, IP do cliente
│   ├── accounts.py         # Plano ativo e emissão da sessão
│   ├── uploads.py          # Upload e conversão de DICOM
│   ├── costs.py            # Orçamento de custo e cota do trial
│   ├── presentation.py     # Formatação e mensagens ao usuário
│   ├── hooks.py            # before/after request, CSRF, cabeçalhos de segurança
│   ├── access_log.py       # Log de acesso assíncrono
│   ├── extensions.py       # Rate limiter
│   └── config.py           # Ambiente, planos, chaves e modelos
├── core/                   # Regras sem dependência do Flask
│   ├── analyzer.py         # Análise com Gemini/Claude, consenso, normalização de imagem
│   ├── reference_images.py # Detecção da região e imagens de referência
│   ├── db.py               # Persistência no Supabase
│   ├── jobs.py             # Fila assíncrona (RQ)
│   ├── cost_tracking.py    # Custo por análise e contadores diários
│   ├── privacy.py          # Remove identificadores do texto clínico (LGPD)
│   ├── asaas.py, mailer.py, email_validation.py, ttl_cache.py, ...
├── templates/, static/     # Interface (Jinja + CSS/JS sem framework)
├── reference_data/         # Imagens normais por região e atlas em PDF
├── supabase_migrations/    # schema.sql consolidado + migrations datadas
├── evals/                  # Avaliação de qualidade dos laudos (casos + pontuação)
└── tests/                  # pytest
```

---

## Banco de Dados

O schema completo do Supabase está documentado em
[`docs/SCHEMA.md`](docs/SCHEMA.md) (diagrama e explicação de cada tabela) e
em [`supabase_migrations/schema.sql`](supabase_migrations/schema.sql) (DDL
consolidado e idempotente — seguro rodar num projeto novo ou no existente).

---

## Instalação e Execução

### Pré-requisitos

- Python 3.12 (versão usada no CI e na Vercel; o código exige no mínimo 3.10)
- Chave de API do Google Gemini — obtenha em [aistudio.google.com](https://aistudio.google.com/app/apikey)
- Opcional: chave da Anthropic (consenso dual), Supabase (persistência), Asaas (pagamentos), Redis (fila)

### 1. Clonar o repositório

```bash
git clone <url-do-repositorio>
cd analise-exame
```

### 2. Criar e ativar o ambiente virtual

```bash
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Instalar dependências

```bash
pip install -r requirements-dev.txt   # desenvolvimento (inclui pytest, ruff, pip-audit)
```

Em produção a Vercel instala `requirements.txt`. Os dois arquivos são
**travados** (todas as versões, inclusive transitivas) e gerados pelo
`pip-compile` — veja [Dependências](#dependências).

### 4. Configurar variáveis de ambiente

```bash
cp .env.example .env
```

Edite o arquivo `.env` e defina sua chave de API:

```env
GEMINI_API_KEY=sua_chave_aqui
```

Se você já usa a nomenclatura da documentação do Google, `GOOGLE_API_KEY`
também é aceito como alias local.

O app usa um orçamento de tempo para manter a geração do consenso abaixo de
90 segundos. O atlas PDF (`reference_data/docs/`) é enviado em toda análise
quando `INCLUDE_REFERENCE_PDF=true` — isso adiciona latência de upload, então
reavalie `ANALYSIS_BUDGET_SECONDS` se notar timeouts:

```env
ANALYSIS_BUDGET_SECONDS=90
INCLUDE_REFERENCE_PDF=true
```

### 5. Iniciar a aplicação

```bash
python app.py
```

Acesse a interface em: `http://localhost:5000`

### 6. Rodar os testes

```bash
pytest -q
```

```bash
ruff check .
```

Os testes não usam rede nem o seu `.env` (ver `tests/conftest.py`).

---

## Privacidade (LGPD)

- **Minimização:** a descrição clínica passa por `core/privacy.py` antes de ir
  aos modelos de IA e ao banco — nome, CPF/CNPJ, CNS, RG, telefone, e-mail e
  data de nascimento viram `[removido]`. O DICOM só guarda hashes dos UIDs.
- **Direitos do titular:** em `/conta` o cliente baixa seus dados (JSON) e
  exclui a conta (senha + confirmação). A exclusão apaga laudos, imagens,
  validações, sessões e logs; as cobranças ficam, sem vínculo, por guarda fiscal.
- **Retenção:** prazos aplicados diariamente pelo banco — ver
  [`docs/SCHEMA.md`](docs/SCHEMA.md#retenção-de-dados-lgpd).

---

## Observabilidade

- **`/admin/custos`** (com o acesso admin): análises, custo estimado e taxa de
  "consenso rápido" por dia e por modo (premium, trial, API).
- **Alerta:** se o consenso rápido passar de `CONSENSUS_FALLBACK_ALERT_RATIO`
  no dia, sai um log de erro (Sentry) uma vez por dia.
- **`trace_id`:** toda resposta traz `X-Trace-Id`; o mesmo id aparece nos logs
  JSON do processo web e do worker que processou o job.

---

## Avaliação de qualidade dos laudos

Antes de trocar prompt, modelo ou orçamento de tempo, rode o conjunto de casos
de referência e compare com a execução anterior — ver
[`evals/README.md`](evals/README.md).

```bash
python -m evals.run_eval --baseline evals/results/<execucao-anterior>.json
```

---

## Dependências

- `requirements.in` / `requirements-dev.in` — dependências **diretas**, com a
  versão mínima aceita. É aqui que se adiciona ou remove um pacote.
- `requirements.txt` / `requirements-dev.txt` — lock gerado; não edite à mão.

Depois de mudar um `.in`, ou para atualizar um pacote específico:

```bash
pip-compile --strip-extras requirements.in
```

```bash
pip-compile --strip-extras requirements-dev.in
```

```bash
pip-compile --strip-extras --upgrade-package flask requirements.in
```

O CI roda `pip-audit -r requirements.txt` e **falha** se o lock tiver alguma
vulnerabilidade conhecida.

---

## Fila de análise assíncrona (opcional)

Por padrão, cada análise roda dentro do próprio ciclo de request/response do
Flask — o cliente espera até ~90s pela resposta. Isso é simples e funciona
bem em baixo volume, mas sob carga cada análise em andamento ocupa um worker
HTTP inteiro, reduzindo a capacidade de atender outras requisições ao mesmo
tempo (mesmo as rápidas, como a página inicial).

Configurando um Redis real em `RATELIMIT_STORAGE_URI` ou
`ANALYSIS_QUEUE_REDIS_URL` (ver `.env.example`), as rotas de análise
(`/analyze`, `/trial/analyze`, `/api/analyze`) passam a enfileirar o
trabalho e responder em milissegundos com um `job_id` — o cliente faz
polling do status até o laudo ficar pronto. **Sem Redis configurado, nada
muda**: as rotas caem automaticamente no caminho síncrono existente.

Quem processa a fila é um worker separado do processo web:

```bash
python worker.py
```

Esse processo precisa rodar em algo que sustente processos de longa duração
(uma VM, um container, um serviço always-on como Railway/Render) — **não
funciona hospedado como função serverless da Vercel**, que não mantém
processos em background entre invocações. Se você deploia só na Vercel sem
um worker rodando em outro lugar, não configure `ANALYSIS_QUEUE_REDIS_URL`
(ou deixe `RATELIMIT_STORAGE_URI=memory://`) para permanecer no caminho
síncrono.

Como o worker é um processo separado sem interface HTTP própria, use o
endpoint `GET /health` do processo web para monitorar as duas dependências
externas que ele compartilha com o app: retorna `200` com
`{"status": "ok", "checks": {"redis": "ok"|"disabled", "supabase": "ok"|"disabled"}}`,
ou `503`/`"status": "degraded"` se alguma dependência configurada não
responder. Configure seu monitor de uptime (Railway/Render/UptimeRobot) para
esse endpoint.

Se `SENTRY_DSN` estiver definido, o worker também reporta exceções dos jobs
de análise ao Sentry com `release` = SHA curto do commit (mesmo valor que o
processo web usa) — permite ver exatamente qual deploy introduziu um erro.
Defina `SENTRY_DSN` também nas variáveis de ambiente de onde o worker roda
(Railway/Render), não só na Vercel.

Detalhes de implementação em [`core/jobs.py`](core/jobs.py).

---

## API REST

A aplicação expõe um endpoint para integração programática.

**Endpoint:** `POST /api/analyze`

A rota exige uma chave de acesso cadastrada em `API_ACCESS_KEYS` (ver
`.env.example`). Sem chaves configuradas, a API fica desligada (401). As
análises usam as chaves Gemini/Anthropic do servidor e entram no orçamento de
custo diário.

```bash
curl -X POST http://localhost:5000/api/analyze \
  -H "X-API-Key: sua_chave_de_acesso" \
  -F "exam_image=@/caminho/para/exame.jpg" \
  -F "description=Ressonância magnética do joelho direito"
```

**Resposta de exemplo:**

```json
{
  "success": true,
  "exam_type": "joelho",
  "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\n...",
  "references_used": 2,
  "model_used": "gemini-2.5-flash"
}
```

---

## Tipos de Exame Suportados

A plataforma é focada em **ortopedia/musculoesquelético**. A região é detectada
pelo nome do arquivo e pela descrição clínica (palavras inteiras, sem acento; a
região com mais termos encontrados vence) e, se nada casar, visualmente pelo
Gemini. Lista completa em `EXAM_TYPE_KEYWORDS` (`core/reference_images.py`).

| Região          | Exemplos de termos detectados                               |
|-----------------|-------------------------------------------------------------|
| `joelho`        | joelho, menisco, patela, LCA, côndilo femoral, platô tibial |
| `coluna`        | coluna, lombar, cervical, hérnia de disco, L4, L5, S1       |
| `ombro`         | ombro, manguito rotador, supraespinhal, acrômio, clavícula  |
| `quadril`       | quadril, acetábulo, cabeça/colo femoral, trocânter, pelve   |
| `pe_tornozelo`  | pé, tornozelo, calcâneo, tálus, metatarso, fáscia plantar   |
| `mao_punho`     | mão, punho, carpo, escafoide, rádio distal, falange         |
| `cotovelo`      | cotovelo, epicôndilo, olécrano, cabeça do rádio, ulna       |
| `geral`         | nenhuma região identificada                                 |

---

## Stack Tecnológica

| Camada     | Tecnologia                              |
|------------|-----------------------------------------|
| Backend         | Python 3, Flask                                                     |
| IA              | Google Gemini (`GEMINI_MODEL`) + Claude (`CLAUDE_MODEL`) em consenso dual |
| Fila (opcional) | Redis + RQ (worker separado — ver "Fila de análise assíncrona")     |
| Banco de dados  | Supabase (Postgres)                                                 |
| Monitoramento   | Sentry (opcional), logging estruturado (JSON)                       |
| Referências     | Wikimedia Commons (domínio público)                                 |
| Frontend        | HTML5, CSS3 (sem frameworks externos)                               |

---

## Licença

Distribuído sob a licença MIT. Consulte o arquivo `LICENSE` para mais informações.
