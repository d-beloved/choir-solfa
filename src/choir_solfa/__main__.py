"""`python -m choir_solfa`: what the desktop shortcut runs."""
import os
import sys
from pathlib import Path

if sys.stdout is None or sys.stderr is None:  # started by pythonw.exe on Windows: no console to print to
    log = Path(os.environ.get("CHOIR_SOLFA_HOME", Path.home() / ".choir-solfa")) / "app.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)

from .cli import main  # noqa: E402

sys.exit(main())
