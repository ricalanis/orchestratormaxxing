#!/usr/bin/env python3
"""Una vuelta del metabolismo de digestión. El worker de 15 min lo invoca.

**Este exit code es la señal, y nada más lo es.** `hermes cron run` imprime
"failed" y devuelve 0 igual (verificado en `hermes_cli/cron.py`), así que
cualquier cosa que lea el wrapper para saber si el loop funcionó se está
mintiendo. Aquí:

    exit 0  — el tick corrió completo. Los fallos a nivel evento son DATOS:
              quedan visibles en la DB (`digest_status`, `attempts`,
              `dead_letter`) y no son un fallo del tick.
    exit 1  — fallo de infraestructura: no se pudo abrir la DB, o el módulo
              reventó. Eso sí es que el loop no corrió.

La salud del loop se lee de la DB, no de aquí: `capture_status()` da la
antigüedad del watermark y la cola por estado. Un tick que no corre en horas se
delata por un watermark viejo, aunque nadie mire este stdout.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    try:
        from dashboard import digestion
    except Exception as e:
        print(json.dumps({"status": "error", "stage": "import",
                          "error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
    try:
        out = digestion.tick()
    except Exception as e:
        print(json.dumps({"status": "error", "stage": "tick",
                          "error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
    # Resumen de una línea: lo que un humano necesita para saber si el
    # metabolismo respiró, sin volcar contenido de conversaciones.
    print(json.dumps({
        "status": out.get("status"),
        "ingested": len((out.get("poll") or {}).get("ingested") or []),
        "receipts": len((out.get("receipts") or {}).get("fetched") or []),
        "digested": len(out.get("digested") or []),
        "digest_errors": len(out.get("digest_errors") or []),
        "worker_down": out.get("worker_down", False),
        "cards_sent": len((out.get("cards") or {}).get("sent") or []),
        "elapsed": out.get("elapsed"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
