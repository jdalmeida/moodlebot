"""DAOs de tarefas e notificações.

Decisões:
- Datas armazenadas como TEXT ISO-8601 UTC. Conversão na fronteira do DAO.
- Confiamos em UNIQUE constraints para idempotência (não tentamos detectar
  duplicação por comparação de timestamps).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from ..models import StatusTarefa, Tarefa, TipoNotif, TipoTarefa
from .conn import get_db


# --------------------------------------------------------------------------- #
# Helpers de serialização
# --------------------------------------------------------------------------- #


def _to_iso(dt: datetime | None) -> str | None:
    """Converte datetime aware para ISO-8601 UTC com sufixo 'Z'.

    Naive datetimes são rejeitados — força a aplicação a sempre carregar tz.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError(f"datetime sem timezone não é permitido: {dt!r}")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    # SQLite às vezes devolve 'YYYY-MM-DD HH:MM:SS' (defaults antigos);
    # também aceitamos o formato com 'T' e 'Z' que escrevemos.
    s = s.replace("Z", "+00:00")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T")
    if "+" not in s and "-" not in s[10:]:
        s += "+00:00"
    return datetime.fromisoformat(s)


def _row_to_tarefa(row: aiosqlite.Row) -> Tarefa:
    return Tarefa(
        id=row["id"],
        moodle_id=row["moodle_id"],
        tipo=TipoTarefa(row["tipo"]),
        curso=row["curso"],
        titulo=row["titulo"],
        url=row["url"],
        prazo=_from_iso(row["prazo"]),
        descricao=row["descricao"],
        status=StatusTarefa(row["status"]),
        primeira_visualizacao=_from_iso(row["primeira_visualizacao"]),
        atualizado_em=_from_iso(row["atualizado_em"]),
    )


# --------------------------------------------------------------------------- #
# DAOs — tarefas
# --------------------------------------------------------------------------- #


async def upsert_tarefa(t: Tarefa) -> tuple[int, bool]:
    """Insere ou atualiza por `moodle_id`.

    Retorna (id_no_banco, recem_criada). `recem_criada` indica se este UPSERT
    inseriu uma linha nova — usado como gatilho da notificação 'nova'.
    """
    conn = await get_db()
    now = _to_iso(datetime.now(timezone.utc))
    sql = """
        INSERT INTO tarefas
            (moodle_id, tipo, curso, titulo, url, prazo, descricao,
             primeira_visualizacao, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(moodle_id) DO UPDATE SET
            titulo        = excluded.titulo,
            curso         = excluded.curso,
            prazo         = excluded.prazo,
            descricao     = COALESCE(excluded.descricao, tarefas.descricao),
            atualizado_em = excluded.atualizado_em
        RETURNING id, (primeira_visualizacao = atualizado_em) AS recem_criada
    """
    params = (
        t.moodle_id,
        t.tipo.value,
        t.curso,
        t.titulo,
        str(t.url),
        _to_iso(t.prazo),
        t.descricao,
        now,
        now,
    )
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    await conn.commit()
    assert row is not None
    return int(row["id"]), bool(row["recem_criada"])


async def list_tarefas_pendentes() -> list[Tarefa]:
    conn = await get_db()
    sql = """
        SELECT * FROM tarefas
        WHERE status = 'pendente'
        ORDER BY
            CASE WHEN prazo IS NULL THEN 1 ELSE 0 END,
            prazo ASC,
            atualizado_em DESC
    """
    async with conn.execute(sql) as cur:
        rows = await cur.fetchall()
    return [_row_to_tarefa(r) for r in rows]


async def get_tarefa(tarefa_id: int) -> Tarefa | None:
    """Busca uma tarefa pelo id interno."""
    conn = await get_db()
    async with conn.execute("SELECT * FROM tarefas WHERE id = ?", (tarefa_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_tarefa(row) if row else None


async def tarefas_pendentes_sem_notif_nova() -> list[Tarefa]:
    """Tarefas pendentes que nunca receberam a notificação 'nova'.

    Confiamos só na UNIQUE constraint para idempotência — não tentamos
    detectar 'inserção recente' por comparação de timestamps porque o UPSERT
    sempre bumpa `atualizado_em` (decisão proposital para manter o registro
    sincronizado com o estado do Moodle).

    Efeito colateral conhecido: no primeiro deploy, todas as tarefas
    pendentes existentes serão notificadas em rajada. Aceitável nesta fase;
    pode ser silenciado seedando `notificacoes_enviadas` manualmente.
    """
    conn = await get_db()
    sql = """
        SELECT t.* FROM tarefas t
        LEFT JOIN notificacoes_enviadas n
               ON n.tarefa_id = t.id AND n.tipo_notif = 'nova'
        WHERE t.status = 'pendente'
          AND n.id IS NULL
    """
    async with conn.execute(sql) as cur:
        rows = await cur.fetchall()
    return [_row_to_tarefa(r) for r in rows]


async def tarefas_precisando_lembrete(
    tipo: TipoNotif,
    janela_inicio: datetime,
    janela_fim: datetime,
) -> list[Tarefa]:
    """Retorna tarefas pendentes cujo prazo cai em [janela_inicio, janela_fim)
    e que ainda não receberam essa notificação."""
    if tipo not in (TipoNotif.LEMBRETE_24H, TipoNotif.LEMBRETE_2H):
        raise ValueError(f"Tipo de lembrete inválido: {tipo}")
    conn = await get_db()
    sql = """
        SELECT t.* FROM tarefas t
        LEFT JOIN notificacoes_enviadas n
               ON n.tarefa_id = t.id AND n.tipo_notif = ?
        WHERE t.status = 'pendente'
          AND t.prazo IS NOT NULL
          AND t.prazo >= ?
          AND t.prazo <  ?
          AND n.id IS NULL
    """
    params = (tipo.value, _to_iso(janela_inicio), _to_iso(janela_fim))
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_tarefa(r) for r in rows]


# --------------------------------------------------------------------------- #
# DAOs — notificações
# --------------------------------------------------------------------------- #


async def marcar_notificacao_enviada(
    tarefa_id: int,
    tipo: TipoNotif,
    telegram_chat_id: int | None = None,
    telegram_message_id: int | None = None,
) -> bool:
    """Registra envio. Retorna False se já existia (UNIQUE violado)."""
    conn = await get_db()
    sql = """
        INSERT INTO notificacoes_enviadas
            (tarefa_id, tipo_notif, telegram_chat_id, telegram_message_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tarefa_id, tipo_notif) DO NOTHING
    """
    async with conn.execute(
        sql, (tarefa_id, tipo.value, telegram_chat_id, telegram_message_id)
    ) as cur:
        inserted = cur.rowcount == 1
    await conn.commit()
    return inserted


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    """Helper de debug — não usar em código de produção."""
    conn = await get_db()
    async with conn.execute(sql, tuple(params)) as cur:
        return list(await cur.fetchall())
