"""Camada de banco — aiosqlite + DDL + DAOs.

Estrutura:
- `conn`: conexão singleton + carga do schema.
- `tarefa`: DAOs de tarefas e notificações (estilo função).
- `repository`: repos de Rascunho e Conversa (estilo classe — fase 2).
- `schema.sql`: DDL single-source-of-truth.

Re-exporta tudo para que `from src.db import upsert_tarefa` continue
funcionando depois do refactor de fase 2.
"""

from __future__ import annotations

from .conn import close_db, get_db, init_schema
from .repository import ConversaRepo, RascunhoRepo
from .tarefa import (
    fetch_all,
    get_tarefa,
    list_tarefas_pendentes,
    marcar_notificacao_enviada,
    tarefas_pendentes_sem_notif_nova,
    tarefas_precisando_lembrete,
    upsert_tarefa,
)


__all__ = [
    "ConversaRepo",
    "RascunhoRepo",
    "close_db",
    "fetch_all",
    "get_db",
    "get_tarefa",
    "init_schema",
    "list_tarefas_pendentes",
    "marcar_notificacao_enviada",
    "tarefas_pendentes_sem_notif_nova",
    "tarefas_precisando_lembrete",
    "upsert_tarefa",
]
