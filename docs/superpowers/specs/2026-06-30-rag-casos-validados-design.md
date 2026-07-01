# RAG com casos validados — design

**Data:** 2026-06-30
**Status:** aprovado para planejamento

## Contexto e motivação

O sistema já coleta feedback e validação clínica formal (`feedbacks_ia`, `diagnosticos_validados`) através do dashboard da clínica, mas esses dados hoje só alimentam métricas (`montar_metricas_dashboard`) — não influenciam análises futuras. O pipeline de análise (`core/analyzer.py`) já injeta contexto de referência estático por tipo de exame (imagens normais em `reference_data/<tipo>/`, atlas em PDF), mas esse material nunca muda com o uso da plataforma.

O objetivo é fechar esse loop: usar diagnósticos validados por especialistas, com alta concordância com a IA, como exemplos de calibração (few-shot) nas próximas análises do mesmo tipo de exame — sem fine-tuning, sem nova infraestrutura de ML.

**Restrição importante descoberta na exploração:** as imagens dos exames dos pacientes não são persistidas em lugar recuperável — `imagens_exame` guarda apenas metadados (mime, tamanho, hash, origem), nunca os bytes da imagem. Portanto o RAG é necessariamente **baseado em texto** (o parecer validado e a lista de achados), não em reprocessar imagens de casos anteriores.

## Decisões (validadas com o usuário)

1. **Filtro de qualidade:** só entram como exemplo os registros de `diagnosticos_validados` com `grau_concordancia >= 80`.
2. **Granularidade de match:** por `tipo_exame` (as 7 categorias já usadas por `reference_images.py` — joelho, coluna, ombro, quadril, pe_tornozelo, mao_punho, cotovelo — mais `geral`). Sem embeddings/pgvector nesta fase.
3. **Ponto de injeção:** nas duas análises independentes (Gemini e Claude), no mesmo estágio em que hoje são injetadas as `reference_images` — não na síntese.
4. **Seleção:** 1 caso por análise — o mais recente (`created_at desc`) entre os que passam no filtro de concordância.
5. **Sanitização:** nenhuma. O campo `diagnostico_final` já é escrito pelo médico como parecer clínico, sem dados de identificação do paciente; será reaproveitado como está.
6. **Mecânica de busca:** cache em memória por `tipo_exame`, TTL de 15 minutos, no mesmo padrão já usado para `_pdf_uri_cache` em `analyzer.py`. Evita uma consulta síncrona ao Supabase em toda análise, dentro do orçamento de tempo apertado (~22s) do pipeline.

## Arquitetura

### Novo: `core/db.py` — `buscar_caso_validado_referencia(tipo_exame, min_concordancia=80)`

- Consulta `diagnosticos_validados` filtrando `tipo_exame` e `grau_concordancia >= min_concordancia`, ordenando por `created_at desc`, `limit(1)`.
- Retorna um `dict` com `diagnostico_final`, `achados_corretos`, `achados_perdidos`, `achados_incorretos`, `grau_concordancia`, `created_at`, ou `None` se não houver correspondência ou o Supabase não estiver configurado.
- Cache em memória (`dict` de módulo, mesmo padrão de `_pdf_uri_cache`): chave `tipo_exame`, valor `{"case": ..., "expires_at": ...}`. TTL de 900s. Resultados vazios (`None`) também são cacheados, para não martelar o Supabase quando não há casos daquele tipo.
- Segue o padrão "fire-and-forget" do restante do arquivo: qualquer exceção é capturada e logada, nunca propagada — a análise deve seguir normalmente sem o exemplo em caso de falha.

### Novo: `core/analyzer.py` — `_format_validated_case_reference(case)`

- Recebe o dict retornado por `buscar_caso_validado_referencia` (ou `None`) e formata um bloco de texto rotulado, ex.:
  ```
  **CASO CLÍNICO VALIDADO POR ESPECIALISTA (referência de calibração — mesmo tipo de exame, concordância {grau}%):**
  {diagnostico_final}
  Achados confirmados neste caso de referência: {achados_corretos joined}
  ```
- Retorna `None` se `case` for `None` ou se `diagnostico_final` estiver vazio.

### Alterações em `analyzer.py`

- `analyze_exam` e `analyze_exam_from_bytes`: depois de resolver `exam_type`, chamar `db.buscar_caso_validado_referencia(exam_type)` e formatar o texto antes de montar os parts/content.
- `_build_content_parts` (Gemini) e `_build_claude_content` (Claude): novo parâmetro `validated_case_text: str | None`, adicionado como bloco de texto logo após o bloco de `reference_images` e antes do bloco do exame do paciente — mesma posição lógica em ambas as funções.
- `build_analysis_prompt`: atualizar a lista "Você recebeu (nesta ordem)" para mencionar o novo bloco, quando presente, mantendo o modelo ciente da natureza/origem do exemplo (parecer de especialista, não achado do paciente atual).
- Import de `core.db` em `analyzer.py` (hoje não importado) — checar import circular: `db.py` não importa `analyzer.py`, então é seguro.

## Fluxo de dados

```
analyze_exam(...)
  → detect exam_type
  → db.buscar_caso_validado_referencia(exam_type)   [cache 15min, fire-and-forget]
      → None (sem Supabase, sem casos, ou erro) → segue sem exemplo, como hoje
      → dict → _format_validated_case_reference(...) → texto
  → _build_content_parts(..., validated_case_text)   → Gemini
  → _build_claude_content(..., validated_case_text)  → Claude
  → (síntese não recebe o texto — só os dois laudos independentes)
```

## Tratamento de erros

- Supabase indisponível/erro de query: capturado em `buscar_caso_validado_referencia`, loga e retorna `None`. Nenhuma exceção sobe para `analyze_exam`.
- Nenhum caso validado para o tipo de exame (comum no início de uso): retorna `None`, comportamento idêntico ao atual (sem exemplo).
- Cache: falha ao popular o cache não deve quebrar a análise — se `_get_client()` retornar `None`, o cache guarda `None` normalmente e a próxima chamada reusa até o TTL expirar.

## Testes

O projeto não tem suíte de testes automatizados hoje (`analise-exame` não tem diretório `tests/`). Escopo de verificação para este trabalho:
- Teste manual: validar um diagnóstico com concordância ≥80% via `dashboard_clinica` → `/validacao-clinica`, depois rodar uma nova análise do mesmo `tipo_exame` e confirmar (via logs `print`) que o caso foi encontrado, cacheado e incluído no prompt enviado a Gemini e Claude.
- Teste manual do caminho de fallback: tipo de exame sem nenhum caso validado (ou Supabase desconfigurado) → análise segue normalmente, sem bloco extra no prompt, sem erro.
- Não introduzir testes automatizados novos além do padrão existente do projeto (nenhum framework de teste configurado).

## Fora de escopo (explicitamente adiado)

- Similaridade semântica / embeddings (pgvector) para casar casos por conteúdo clínico, não só por categoria.
- Múltiplos exemplares por análise.
- Sanitização/anonimização do texto do parecer validado.
- Uso do exemplo na etapa de síntese/consenso.
- Job agendado para pré-computar/atualizar exemplares.
