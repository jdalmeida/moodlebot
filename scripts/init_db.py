"""Inicializa o schema SQLite. Idempotente.

Uso:
    python scripts/init_db.py
"""

from __future__ import annotations

import asyncio

import _bootstrap  # noqa: F401  — adiciona a raiz do repo ao sys.path

from src.config import settings
from src.db import close_db, init_schema
from src.logging_setup import setup_logging


async def _main() -> None:
    setup_logging()
    await init_schema()
    print(f"OK: banco em {settings.database_path}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(_main())
