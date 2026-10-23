-- Three Health AI — marcação dos achados nas imagens do exame
--
-- Cada laudo guarda a lista de achados localizados nas imagens (core/annotations.py):
--   [{"numero": 1, "imagem": 1, "estrutura": "...", "local": "...",
--     "certeza": "forte" | "moderado" | "baixo", "box_2d": [ymin, xmin, ymax, xmax]}]
-- Coordenadas normalizadas de 0 a 1000. As imagens em si continuam sem ser
-- guardadas; no laudo salvo aparece a lista de localizações.
--
-- Idempotente. Rode ANTES do deploy: buscar_analise já seleciona a coluna.

alter table public.analises
    add column if not exists marcacoes jsonb;

comment on column public.analises.marcacoes is
    'Achados localizados nas imagens: estrutura, local exato, certeza (forte/moderado/baixo) e caixa box_2d 0-1000.';
