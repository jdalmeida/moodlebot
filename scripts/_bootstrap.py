"""Helper compartilhado pelos scripts em `scripts/`.

Importar antes de qualquer outro módulo do projeto para garantir que `src/`
está no `sys.path` (já que o projeto não usa `pip install -e .`).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
