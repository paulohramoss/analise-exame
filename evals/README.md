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
| 35% | achados esperados presentes (com sinônimos, sem acento) |
| 15% | **sinais de alerta esperados presentes na seção 0** |
| 15% | as 7 seções obrigatórias do laudo presentes (0 a 6) |
| 15% | região anatômica detectada correta |
| 10% | achados com rótulo de evidência ([FORTE]/[MODERADO]) |
| 10% | nenhum achado proibido na **impressão** |

**Sinal de alerta perdido reprova o caso**, qualquer que seja a nota: é o falso
negativo que machuca paciente (fratura, lesão agressiva, infecção, compressão
neural…). Em casos sem alerta (`"expected_red_flags": []`), um alerta emitido
mesmo assim aparece como "alerta a mais": é registrado, mas não tira nota.
O resumo da execução traz a **sensibilidade para sinais de alerta**.

> Execuções salvas antes da seção 0 existir perdem pontos em "seções" ao serem
> repontuadas; para comparar versões, gere uma linha de base nova.

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

## Protocolo mínimo antes de uso clínico

Dois casos de exemplo não medem nada: eles mostram o formato. Para poder dizer
a um cliente "a ferramenta acerta X%", o conjunto precisa de:

1. **Pelo menos 50 casos, idealmente 100**, distribuídos pelas 7 regiões e pelas
   modalidades que os clientes mais enviam (RX e RM primeiro).
2. **Padrão-ouro de radiologista** (laudo original ou revisão por especialista)
   para cada caso. Sem padrão-ouro, o caso não entra.
3. **Ao menos 20% de casos com sinal de alerta** (`expected_red_flags`):
   fratura sutil, fratura de escafoide, fratura por estresse, lesão lítica,
   espondilodiscite, compressão medular, osteonecrose, rotura completa de
   tendão de Aquiles.
4. **Ao menos 20% de exames normais** (`"expected_red_flags": []`, sem achados
   esperados), para medir alerta a mais e achado inventado.
5. Casos difíceis de propósito: imagem de baixa qualidade, região ambígua,
   print de tela com régua e texto, PDF de laudo.

Números a acompanhar a cada mudança de prompt ou modelo: sensibilidade para
sinais de alerta (meta: 100% nos casos do conjunto), casos aprovados e média.
Guarde a execução de referência e rode com `--baseline` antes de publicar.
