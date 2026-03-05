# reference_data/docs — Atlas e Livros de Referência (PDF)

Coloque aqui os arquivos PDF de atlas anatômicos e livros de radiologia.
Eles serão enviados diretamente ao Gemini como base de conhecimento para cada análise.

## Como adicionar

1. Coloque o PDF nesta pasta:
   ```
   reference_data/docs/anatomia-do-sistema-locomotor-e-atlas-fotografico.pdf
   reference_data/docs/gray-anatomy-regions.pdf
   ```
2. Faça commit e push — o Vercel irá incluí-los no deploy automaticamente.

## Limite de tamanho

- O Gemini aceita PDFs via inline data até **~20 MB** por arquivo.
- Arquivos maiores: considere dividir por capítulos ou usar apenas as seções relevantes.
- O sistema usa **no máximo 1 PDF por análise** para evitar exceder o contexto.

## Sugestões de conteúdo

| Arquivo | Uso |
|---------|-----|
| Atlas do sistema locomotor | Joelho, ombro, coluna, quadril |
| Atlas de radiologia torácica | Raio-X e TC de tórax |
| Atlas de neuroanatomia | RM de crânio e coluna |
| Netter (capítulos específicos) | Referência anatômica geral |
