"""
Roda a análise completa (laudo + marcação dos achados) numa imagem local e salva
o resultado para conferir a precisão dos círculos.

Uso:
    python scripts/testar_marcacoes.py exame.jpg [--descricao "RM joelho D, dor medial"] [--saida pasta]

Precisa de GEMINI_API_KEY (e, opcional, ANTHROPIC_API_KEY para o consenso) no
ambiente ou no .env. Gera na pasta de saída:
    <nome>-marcado.png   imagem com os círculos (verde/amarelo/vermelho) numerados
    <nome>-laudo.md      laudo gerado
    <nome>-marcacoes.json  marcações (estrutura, local, certeza, box_2d 0-1000)
Nada é gravado no banco: a persistência é desligada.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa: E402

load_dotenv()
os.environ["SUPABASE_URL"] = ""  # nada de banco num teste local

from core import analyzer  # noqa: E402

CORES = {"forte": (22, 163, 74), "moderado": (234, 179, 8), "baixo": (220, 38, 38)}


def desenhar(image_path: Path, marcacoes: list[dict], destino: Path) -> None:
    """Mesmo desenho de static/laudo-marcacoes.js, em PIL."""
    img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    w, h = img.size
    short = min(w, h)
    draw = ImageDraw.Draw(img)
    line = max(2, round(short * 0.006))
    badge = max(10, round(short * 0.028))
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", round(badge * 1.15))
    except OSError:
        font = ImageFont.load_default()
    for m in reversed(marcacoes):
        ymin, xmin, ymax, xmax = m["box_2d"]
        cx, cy = (xmin + xmax) / 2000 * w, (ymin + ymax) / 2000 * h
        rx = max((xmax - xmin) / 2000 * w * 1.08, short * 0.018)
        ry = max((ymax - ymin) / 2000 * h * 1.08, short * 0.018)
        box = (cx - rx, cy - ry, cx + rx, cy + ry)
        draw.ellipse(box, outline=(0, 0, 0), width=round(line * 2.2))
        draw.ellipse(box, outline=CORES[m["certeza"]], width=line)
        bx = min(max(cx + rx * 0.72, badge), w - badge)
        by = min(max(cy - ry * 0.72, badge), h - badge)
        draw.ellipse((bx - badge, by - badge, bx + badge, by + badge), fill=CORES[m["certeza"]], outline="white", width=2)
        texto = (31, 41, 55) if m["certeza"] == "moderado" else (255, 255, 255)
        draw.text((bx, by), str(m["numero"]), fill=texto, font=font, anchor="mm")
    img.save(destino)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("imagem", type=Path)
    parser.add_argument("--descricao", default="")
    parser.add_argument("--saida", type=Path, default=Path("."))
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not anthropic_key:
        sys.exit("Defina GEMINI_API_KEY (e, se quiser o consenso, ANTHROPIC_API_KEY).")

    raw = args.imagem.read_bytes()
    mime = "image/png" if args.imagem.suffix.lower() == ".png" else "image/jpeg"
    result = analyzer.analyze_exam_from_bytes(
        [(raw, mime)], args.imagem.name, api_key,
        user_description=args.descricao,
        model_name=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        anthropic_api_key=anthropic_key,
        on_progress=lambda etapa: print(f"… {etapa}"),
    )

    args.saida.mkdir(parents=True, exist_ok=True)
    base = args.saida / args.imagem.stem
    marcacoes = result.get("annotations") or []
    desenhar(args.imagem, marcacoes, base.with_name(base.name + "-marcado.png"))
    base.with_name(base.name + "-laudo.md").write_text(result["analysis"], encoding="utf-8")
    base.with_name(base.name + "-marcacoes.json").write_text(
        json.dumps(marcacoes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nRegião: {result['exam_type']} | modelo: {result['model_used']}")
    for m in marcacoes:
        print(f"  {m['numero']}. [{m['certeza']}] {m['estrutura']} — {m['local']} {m['box_2d']}")
    if not marcacoes:
        print("  Nenhum achado localizado.")
    print(f"\nArquivos em {args.saida.resolve()}")


if __name__ == "__main__":
    main()
