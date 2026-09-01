# --- orchestratormaxxing: delegación del tick de digestión -------------------------
#
# QUÉ: reemplaza el bloque final de `~/.hermes/scripts/memory-consolidation-worker.py`
#      por este. Ese worker ya corre cada 15 min como cron job de Hermes, así que
#      el tick de digestión viaja con él en vez de pelear por un segundo
#      scheduler — un cron propio duplicaría el reloj y ninguno sabría del otro.
#
# POR QUÉ un reemplazo y no un append: el worker termina en `exit(main())`, que
# no regresa. Cualquier cosa añadida DESPUÉS de esa línea jamás se ejecuta.
#
# CÓMO instalarlo (paso de operador, una sola vez — `~/.hermes` está fuera del repo):
#     1. Abre `~/.hermes/scripts/memory-consolidation-worker.py`
#     2. Sustituye sus últimas dos líneas (`if __name__ == "__main__":` / `exit(main())`)
#        por todo lo que sigue a este comentario.
#
# El resultado del tick NO altera el estado del worker: la consolidación de
# memoria y la digestión son trabajos distintos y uno caído no debe teñir al
# otro. Pero tampoco se traga en silencio — un tick que falla deja su línea en
# stderr, y su verdadera salud se lee de la DB (`capture_status()`: antigüedad
# del watermark y cola por estado), no de este wrapper.
#
# El timeout de 540 s es menor que la cadencia de 15 min a propósito: dos ticks
# encimados competirían por los mismos leases. El propio tick se acota con
# TICK_BUDGET_SECONDS (480 s) para terminar por su cuenta ANTES de este techo,
# en vez de que lo maten a media digestión.

if __name__ == "__main__":
    rc = main()
    try:
        import subprocess as _sp
        from pathlib import Path as _P
        _repo = _P.home() / "dev" / "orchestratormaxxing" / "orchestrator"
        _out = _sp.run([str(_repo / ".venv" / "bin" / "python"),
                        str(_repo / "scripts" / "digestion-tick.py")],
                       capture_output=True, text=True, timeout=540)
        if _out.returncode != 0:
            print(f"[digestion-tick] exit={_out.returncode} {(_out.stderr or '')[:300]}",
                  file=__import__("sys").stderr)
        elif _out.stdout.strip():
            print(f"[digestion-tick] {_out.stdout.strip()}")
    except Exception as _e:
        print(f"[digestion-tick] no corrió: {type(_e).__name__}: {_e}",
              file=__import__("sys").stderr)
    exit(rc)
