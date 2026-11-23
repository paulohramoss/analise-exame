"""
Reprocessa as análises da fila que falharam por erro de IA ou de infraestrutura.

    python -m scripts.reprocessar_fila [--limite 100]

Ficam de fora as que falharam por causa do arquivo (não é exame, pedido em vez
de resultado…): falhariam de novo. Precisa do mesmo Redis do worker.
"""

import argparse
import sys

from core import jobs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limite", type=int, default=100)
    args = parser.parse_args(argv)
    stats = jobs.requeue_failed(limit=args.limite)
    if not stats["disponivel"]:
        print("Redis da fila não configurado ou fora do ar.", file=sys.stderr)
        return 2
    print(f"Reprocessadas: {stats['reprocessados']} | ignoradas (erro do arquivo): {stats['ignorados']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
