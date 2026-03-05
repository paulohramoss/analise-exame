# Imagens de Referência — Three Health

Esta pasta contém as imagens que servem de **base anatômica** para a análise da IA.
Quanto mais imagens normais de qualidade forem adicionadas, mais precisa será a comparação.

## Como adicionar imagens

Coloque as imagens de referência (exames normais) na subpasta correspondente ao tipo de exame.
A IA usa essas imagens para comparar com o exame do paciente.

```
reference_data/
├── raio_x_torax/          → Radiografias de tórax normais (PA e lateral)
├── ressonancia_cerebro/   → RM de crânio normal (axial, coronal, sagital)
├── ressonancia_joelho/    → RM de joelho normal (sagital, coronal, axial)
├── ressonancia_coluna/    → RM de coluna normal (lombar, cervical, torácica)
├── tomografia_cranio/     → TC de crânio normal
├── ultrassonografia/      → Ultrassonografias normais
├── mamografia/            → Mamografias normais (CC e MLO)
└── geral/                 → Imagens de referência gerais
```

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
