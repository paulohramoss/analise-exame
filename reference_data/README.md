# Imagens de Referência — Three Health

Esta pasta contém as imagens que servem de **base anatômica** para a análise da IA.
Quanto mais imagens normais de qualidade forem adicionadas, mais precisa será a comparação.

## Como adicionar imagens

Coloque as imagens de referência (exames normais) na subpasta da região anatômica.
O nome da pasta é **exatamente** o tipo de exame detectado pelo sistema
(`EXAM_TYPE_KEYWORDS` em `core/reference_images.py`) — uma pasta com outro nome
nunca é lida.

```
reference_data/
├── joelho/         → RM / RX de joelho normal (sagital, coronal, axial)
├── coluna/         → RM / RX de coluna normal (lombar, cervical, torácica)
├── ombro/          → RM / RX de ombro normal
├── quadril/        → RM / RX de quadril e pelve normais
├── pe_tornozelo/   → RM / RX de pé e tornozelo normais
├── mao_punho/      → RM / RX de mão e punho normais
├── cotovelo/       → RM / RX de cotovelo normal
├── geral/          → usada só quando a região não pôde ser identificada
└── docs/           → atlas em PDF (ver docs/README.md)
```

Use **imagens de exame**, não diagramas ou desenhos esquemáticos: o modelo
compara o exame do paciente com elas pixel a pixel.

Sem imagem local, joelho e ombro baixam uma referência pública da Wikimedia;
as demais regiões seguem **sem** comparação visual (melhor do que comparar com
a região errada).

## Formatos aceitos

- `.jpg` / `.jpeg`
- `.png`
- `.webp`

## Sugestões de fontes públicas

- **Radiopaedia.org** — casos com licença Creative Commons
- **NIH NLM MedPix** — banco de imagens médicas público (medpix.nlm.nih.gov)
- **The Cancer Imaging Archive (TCIA)** — imagens oncológicas públicas
- **OpenI NIH** — chest X-rays com laudos abertos

## Nomenclatura sugerida

```
normal_01.jpg
normal_02.jpg
atlas_gray_torax_01.jpg   ← identifique a fonte quando possível
netter_joelho_sagital.jpg
```

> A IA processa até **2 imagens de referência** por análise (as primeiras em ordem alfabética).
> Para trocar quais imagens são usadas, basta renomeá-las.
