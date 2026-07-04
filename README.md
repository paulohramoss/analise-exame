# MedAI Analyzer

> Analisador inteligente de exames médicos por imagem, com geração automatizada de laudos estruturados via Google Gemini.

---

## Visão Geral

O **MedAI Analyzer** é uma aplicação web que utiliza a API multimodal do **Google Gemini** para analisar imagens de exames médicos (Ressonância Magnética, Raio-X, Tomografia, entre outros). O sistema detecta automaticamente o tipo de exame, busca imagens de referência normais para comparação e gera um laudo estruturado com achados clínicos, hipóteses diagnósticas e recomendações.

> **Aviso clínico:** Esta ferramenta destina-se exclusivamente ao uso como auxiliar por profissionais de saúde habilitados. **Não substitui** o diagnóstico médico especializado.

---

## Funcionalidades

- Upload de imagens nos formatos PNG, JPG, JPEG e WEBP
- Detecção automática do tipo de exame a partir da descrição
- Busca de imagens de referência normais (Wikimedia Commons) para análise comparativa
- Geração de laudo estruturado via Google Gemini (modelo multimodal)
- Interface web responsiva, sem dependências de frameworks externos
- API REST para integração programática com outros sistemas

---

## Estrutura do Projeto

```
analise-exame/
├── app.py                    # Aplicação Flask — rotas e ponto de entrada
├── requirements.txt          # Dependências Python
├── .env.example              # Template de variáveis de ambiente
├── core/
│   ├── analyzer.py           # Lógica de análise com a API do Gemini
│   └── reference_images.py   # Busca e gerenciamento de imagens de referência
├── templates/
│   ├── index.html            # Página de upload de exame
│   └── result.html           # Página de exibição do laudo
└── static/
    └── style.css             # Estilos da interface
```

> A árvore acima é ilustrativa. O `core/` também tem `db.py` (Supabase),
> `asaas.py` (pagamentos), `cost_tracking.py`, `logging_config.py` e
> `jobs.py` (fila assíncrona) — veja os arquivos para detalhes.

---

## Banco de Dados

O schema completo do Supabase está documentado em
[`docs/SCHEMA.md`](docs/SCHEMA.md) (diagrama e explicação de cada tabela) e
em [`supabase_migrations/schema.sql`](supabase_migrations/schema.sql) (DDL
consolidado e idempotente — seguro rodar num projeto novo ou no existente).

---

## Instalação e Execução

### Pré-requisitos

- Python 3.9+
- Chave de API do Google Gemini — obtenha em [aistudio.google.com](https://aistudio.google.com/app/apikey)

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
pip install -r requirements.txt
```

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

```bash
curl -X POST http://localhost:5000/api/analyze \
  -H "X-API-Key: sua_chave_gemini" \
  -F "exam_image=@/caminho/para/exame.jpg" \
  -F "description=Ressonância magnética do joelho direito"
```

**Resposta de exemplo:**

```json
{
  "success": true,
  "exam_type": "ressonancia_joelho",
  "analysis": "## 1. IDENTIFICAÇÃO DO EXAME\n...",
  "references_used": 2,
  "model_used": "gemini-2.5-flash"
}
```

---

## Tipos de Exame Suportados

| Tipo de Exame         | Palavras-chave detectadas             |
|-----------------------|---------------------------------------|
| Ressonância Cerebral  | `cerebro`, `cranio`, `brain`, `mri head` |
| Ressonância Joelho    | `joelho`, `knee`, `tibial`, `femoral` |
| Raio-X Tórax          | `torax`, `pulm`, `chest`, `xray`      |
| Ressonância Coluna    | `coluna`, `lombar`, `cervical`, `spine` |
| Tomografia Crânio     | `tomografia`, `ct scan`, `tac`        |

---

## Stack Tecnológica

| Camada     | Tecnologia                              |
|------------|-----------------------------------------|
| Backend         | Python 3, Flask                                                     |
| IA              | Google Gemini 2.5 Flash (multimodal), Claude Sonnet (consenso dual) |
| Fila (opcional) | Redis + RQ (worker separado — ver "Fila de análise assíncrona")     |
| Banco de dados  | Supabase (Postgres)                                                 |
| Monitoramento   | Sentry (opcional), logging estruturado (JSON)                       |
| Referências     | Wikimedia Commons (domínio público)                                 |
| Frontend        | HTML5, CSS3 (sem frameworks externos)                               |

---

## Licença

Distribuído sob a licença MIT. Consulte o arquivo `LICENSE` para mais informações.
