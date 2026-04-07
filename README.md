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

### 5. Iniciar a aplicação

```bash
python app.py
```

Acesse a interface em: `http://localhost:5000`

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
| Backend    | Python 3, Flask                         |
| IA         | Google Gemini 2.5 Flash (multimodal)    |
| Referências | Wikimedia Commons (domínio público)    |
| Frontend   | HTML5, CSS3 (sem frameworks externos)   |

---

## Licença

Distribuído sob a licença MIT. Consulte o arquivo `LICENSE` para mais informações.
