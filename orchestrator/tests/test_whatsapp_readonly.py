"""La postura no-send de WhatsApp, sostenida por lint en vez de por disciplina.

El operador acepta un riesgo de ban bajo-pero-no-cero sobre el número principal de su
negocio, y ese cálculo depende de UNA cosa: que este sistema nunca envíe. Toda la
evidencia recogida dice que los vectores de ban documentados mecánicamente son
vectores de ENVÍO — mensajes a no-contactos, ráfagas, status, difusión.

Una regla escrita en un comentario se erosiona; un lint no. Estos tests fallan si
alguien agrega un `send` a cualquier invocación de wacli del repositorio, aunque
lo haga con buena intención.

Nota de precisión, medida contra v0.15.2 el 2026-08-04: `--read-only` **rechaza
`sync`**, porque sync escribe el store local por definición. Así que el demonio
NO puede llevar esa bandera y sería falso afirmar que "la herramienta impide
escribir". Lo que sí se sostiene está en `_FORBIDDEN` + el modo del SQLite + el
hecho de que `sync` no envía.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Subcomandos de wacli que tocan WhatsApp hacia afuera. Ninguno tiene por qué
# aparecer nunca: `presence` difunde "en línea" y le cambia las notificaciones al
# teléfono del operador; `mark-read` altera el estado que él ve; el resto envían.
_FORBIDDEN = ("send", "presence", "mark-read", "markread", "reply", "react",
              "status", "broadcast")

# Dónde puede aparecer una invocación de wacli.
_SCAN = ["dashboard", "scripts", "deploy", "mcp_server.py", "mcp_sse_server.py"]


def _repo_files():
    for entry in _SCAN:
        p = REPO / entry
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.suffix in (".py", ".service", ".sh", ".tmpl") and "__pycache__" not in str(f):
                    yield f


class NoSendPathExists(unittest.TestCase):
    def test_no_wacli_invocation_names_a_write_subcommand(self):
        """El lint se acota a LÍNEAS que mencionan wacli: un grep suelto de
        'send' chocaría con `hermes send`, que es legítimo y necesario."""
        offenders = []
        for f in _repo_files():
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                if "wacli" not in low or low.lstrip().startswith("#"):
                    continue
                for word in _FORBIDDEN:
                    if re.search(rf"\b{re.escape(word)}\b", low):
                        offenders.append(f"{f.relative_to(REPO)}:{n}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "invocación de wacli que escribe hacia WhatsApp:\n" + "\n".join(offenders))


class TheUnitCarriesItsMitigations(unittest.TestCase):
    """El unit es donde viven las mitigaciones de ban; si alguien las quita, el
    cálculo de riesgo que el operador aceptó deja de ser cierto."""

    @classmethod
    def setUpClass(cls):
        cls.unit = (REPO / "deploy" / "wacli-sync.service").read_text(encoding="utf-8")

    def test_presence_is_quiet(self):
        """Sin esto el espejo le roba al teléfono la señal de en-línea/leído."""
        self.assertIn("--presence-mode quiet", self.unit)

    def test_storage_is_capped(self):
        self.assertIn("--max-db-size", self.unit)
        self.assertIn("--max-messages", self.unit)

    def test_it_only_ever_syncs(self):
        cmd = self.unit[self.unit.index("ExecStart="):self.unit.index("Restart=")]
        self.assertIn("sync --follow", cmd)
        for word in _FORBIDDEN:
            self.assertNotIn(f" {word} ", cmd, f"el unit no puede invocar `{word}`")

    def test_it_ships_disarmed(self):
        """Emparejar es acto del operador. El archivo se despliega, no se habilita —
        y el comentario dice por qué, para que nadie lo 'complete'."""
        self.assertIn("DESARMADO", self.unit)
        self.assertIn("wacli auth", self.unit)

    def test_the_binary_is_pinned(self):
        """La versión de brew está congelada en 0.2.0 y NO tiene `--read-only`
        ni `--presence-mode`. Apuntar por PATH tomaría esa."""
        self.assertIn("wacli-0.15.2/wacli", self.unit)
        self.assertNotIn("ExecStart=wacli", self.unit)

    def test_the_readonly_nuance_is_written_down(self):
        """`--read-only` rechaza `sync`, así que el demonio no puede llevarla.
        Ese matiz tiene que estar escrito o alguien lo 'arreglará' agregándola y
        el servicio dejará de arrancar."""
        self.assertIn("--read-only", self.unit)
        self.assertIn("rechaza `sync`", self.unit)


if __name__ == "__main__":
    unittest.main()
