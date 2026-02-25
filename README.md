# MedAI Analyzer — Analisador de Exames Médicos com IA

Aplicação web que utiliza a **API do Google Gemini** para analisar imagens de exames médicos (Ressonância Magnética, Raio-X, Tomografia, etc.), comparando com imagens de referência normais e gerando um laudo estruturado.

> **Aviso:** Esta ferramenta é um auxiliar para profissionais de saúde e **não substitui** o diagnóstico de um médico especialista.

## Funcionalidades

- Upload de imagens de exames (PNG, JPG, JPEG, WEBP)
- Detecção automática do tipo de exame
- Busca de imagens de referência normais para comparação
- Análise comparativa via Google Gemini (modelo multimodal)
- Laudo estruturado com achados, hipóteses diagnósticas e recomendações
- Interface web responsiva
- API REST para integração programática

## Estrutura do Projeto

```
analise-exame/
├── app.py                    # Aplicação Flask principal
├── requirements.txt          # Dependências Python
├── .env.example             # Template de variáveis de ambiente
├── core/
│   ├── analyzer.py          # Lógica de análise com Gemini
│   └── reference_images.py  # Gerenciamento de imagens de referência
├── templates/
│   ├── index.html           # Página de upload
│   └── result.html          # Página com o laudo
└── static/
    └── style.css            # Estilos CSS
```

## Como Usar

### 1. Configurar o ambiente

```bash
# Clonar o repositório
git clone <url-do-repositorio>
cd analise-exame

# Criar e ativar ambiente virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Instalar dependências
pip install -r requirements.txt
```

### 2. Configurar a API do Gemini

```bash
# Copiar o arquivo de exemplo
cp .env.example .env

# Editar .env e adicionar sua chave
# Obtenha sua chave em: https://aistudio.google.com/app/apikey
GEMINI_API_KEY=sua_chave_aqui
```

### 3. Executar a aplicação

```bash
python app.py
```

Acesse: `http://localhost:5000`

## API REST

Para integração programática:

```bash
curl -X POST http://localhost:5000/api/analyze \
  -H "X-API-Key: sua_chave_gemini" \
  -F "exam_image=@/caminho/para/exame.jpg" \
  -F "description=Ressonância magnética do joelho direito"
```

**Resposta:**
```json
{
  "success": true,
  "exam_type": "ressonancia_joelho",
  "analysis": "## 1. IDENTIFICAÇÃO DO EXAME...",
  "references_used": 2,
  "model_used": "gemini-1.5-pro"
}
```

## Tipos de Exame Suportados

| Tipo | Palavras-chave detectadas |
|------|--------------------------|
| Ressonância Cerebral | cerebro, cranio, brain, mri head |
| Ressonância Joelho | joelho, knee, tibial, femoral |
| Raio-X Tórax | torax, pulm, chest, xray |
| Ressonância Coluna | coluna, lombar, cervical, spine |
| Tomografia Crânio | tomografia, ct scan, tac |

## Tecnologias

- **Backend:** Python, Flask
- **IA:** Google Gemini 1.5 Pro (multimodal)
- **Imagens de referência:** Wikipedia Commons (domínio público)
- **Frontend:** HTML5, CSS3 (sem frameworks externos)
