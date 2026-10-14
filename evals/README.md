# Avaliação de qualidade dos laudos

Conjunto de casos com resposta conhecida para medir, **antes de publicar**, se
uma mudança de prompt, modelo (`GEMINI_MODEL`, `CLAUDE_MODEL`) ou orçamento de
tempo melhorou ou piorou os laudos.

## Como funciona

Cada caso (`cases/*.json`) aponta para uma ou mais imagens e diz o que o laudo
deve conter. `run_eval.py` gera o laudo de cada caso com a configuração atual e
dá uma nota de 0 a 1 (`scoring.py`):

| Peso | Métrica |
|------|---------|
| 40% | achados esperados presentes (com sinônimos, sem acento) |
| 20% | as 6 seções obrigatórias do laudo presentes |
| 20% | região anatômica detectada correta |
| 10% | achados com rótulo de evidência ([FORTE]/[MODERADO]) |
| 10% | nenhum achado proibido na **impressão diagnóstica** |

A nota mede o que é objetivo. Não substitui a leitura de um especialista.

## Rodando

```bash
python -m evals.run_eval
```

```bash
python -m evals.run_eval --baseline evals/results/<execucao-anterior>.json
```

```bash
python -m evals.run_eval --rescore evals/results/<execucao>.json
```

- O primeiro **chama as APIs de IA** (custa, como uma análise real).
- Com `--baseline`, o comando termina com erro se a média cair mais de 0,05
  ou se algum caso que passava passar a falhar.
- `--rescore` só recalcula as notas de laudos já salvos — útil ao ajustar
  os casos ou a pontuação, sem gastar.

## Montando os casos

- **Imagens não vão para o git** (`evals/images/` e `evals/results/` estão no
  `.gitignore`): use apenas exames **anonimizados** (sem nome, data de
  nascimento ou identificadores gravados na imagem) ou de domínio público /
  Creative Commons (ex.: Radiopaedia, com a licença registrada no caso).
- Prefira casos com laudo de radiologista: os achados esperados vêm dele.
- Cubra as 7 regiões e os erros já observados (ex.: região trocada, achado
  inventado). Comece com 2–3 casos por região.
- `cases/exemplo-joelho-lca.json` mostra o formato completo.
