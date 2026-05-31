"""Conexão SQLite + carregamento do schema.

- Uma conexão única por processo, protegida por asyncio.Lock no `get_db()`.
  SQLite serializa escritas internamente; com WAL múltiplos leitores cabem.
- Schema vive em `schema.sql` ao lado deste módulo — single source of truth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
from loguru import logger

from ..config import settings


_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


_conn: aiosqlite.Connection | None = None
_conn_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    """Retorna a conexão singleton, abrindo-a sob demanda."""
    global _conn
    async with _conn_lock:
        if _conn is None:
            settings.database_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(settings.database_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.commit()
            _conn = conn
            logger.debug("Conexão SQLite aberta em {}", settings.database_path)
        return _conn


async def close_db() -> None:
    global _conn
    async with _conn_lock:
        if _conn is not None:
            await _conn.close()
            _conn = None


async def init_schema() -> None:
    """Cria as tabelas se não existirem. Idempotente.

    Observação: `schema.sql` dropa `rascunhos` antes de recriar — migração da
    fase 2. Idempotência preservada: rodar duas vezes seguidas é seguro
    (apenas re-dropa uma tabela vazia).
    """
    conn = await get_db()
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    await conn.executescript(schema_sql)
    await conn.commit()
    logger.info("Schema SQLite inicializado em {}", settings.database_path)
