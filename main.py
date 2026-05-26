"""Entry point do Moodlebot.

Uso:
    python main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Como não usamos `pip install -e .`, adicionamos a raiz do repo ao sys.path
# para que `src` seja importável como pacote (e os imports relativos
# `from .config import ...` dentro dele funcionem).
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))

from src.app import run  # noqa: E402


if __name__ == "__main__":
    run()
