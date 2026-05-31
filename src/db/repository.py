"""Repos da fase 2 — rascunhos e conversas.

Estilo: métodos estáticos. `get_db()` é singleton, então não há DI a fazer.
Mantém o mesmo padrão dos DAOs em `tarefa.py`, só agrupados em classe para
o domínio ficar legível ("RascunhoRepo.criar", "ConversaRepo.obter").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from ..models import Conversa, EstadoConversa, Rascunho
from .conn import get_db
from .tarefa import _from_iso, _to_iso


def _row_to_rascunho(row: aiosqlite.Row) -> Rascunho:
    return Rascunho(
        id=row["id"],
        tarefa_id=row["tarefa_id"],
        chat_id=row["chat_id"],
        versao=row["versao"],
        conteudo=row["conteudo"],
        prompt_usado=row["prompt_usado"],
        salvo_no_moodle=bool(row["salvo_no_moodle"]),
        criado_em=_from_iso(row["criado_em"]),
    )


def _row_to_conversa(row: aiosqlite.Row) -> Conversa:
    return Conversa(
        chat_id=row["chat_id"],
        tarefa_id=row["tarefa_id"],
        estado=EstadoConversa(row["estado"]),
        contexto_json=row["contexto_json"],
        atualizado_em=_from_iso(row["atualizado_em"]),
    )


class RascunhoRepo:
    """Versionamento de rascunhos por (tarefa, chat).

    Versões monotônicas começando em 1. A coluna `salvo_no_moodle` marca
    quando o submitter confirmou a gravação no Moodle.
    """

    @staticmethod
    async def criar(
        tarefa_id: int,
        chat_id: int,
        conteudo: str,
        prompt_usado: str | None = None,
    ) -> Rascunho:
        """Insere uma nova versão, calculando MAX(versao)+1 atomicamente.

        A computação de `versao` vive na própria query — combinado com
        UNIQUE(tarefa_id, chat_id, versao), dois INSERTs concorrentes não
        produzem versões duplicadas (o segundo falha com IntegrityError).
        """
        conn = await get_db()
        sql = """
            INSERT INTO rascunhos
                (tarefa_id, chat_id, versao, conteudo, prompt_usado, criado_em)
            VALUES (
                ?, ?,
                (SELECT COALESCE(MAX(versao), 0) + 1
                   FROM rascunhos
                  WHERE tarefa_id = ? AND chat_id = ?),
                ?, ?, ?
            )
            RETURNING id, versao, criado_em
        """
        now = _to_iso(datetime.now(timezone.utc))
        params = (
            tarefa_id,
            chat_id,
            tarefa_id,
            chat_id,
            conteudo,
            prompt_usado,
            now,
        )
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        await conn.commit()
        assert row is not None
        return Rascunho(
            id=int(row["id"]),
            tarefa_id=tarefa_id,
            chat_id=chat_id,
            versao=int(row["versao"]),
            conteudo=conteudo,
            prompt_usado=prompt_usado,
            salvo_no_moodle=False,
            criado_em=_from_iso(row["criado_em"]),
        )

    @staticmethod
    async def ativo_para(chat_id: int, tarefa_id: int) -> Rascunho | None:
        """Última versão ainda não-salva. None se nada existir, OU se a
        última versão já foi salva no Moodle (fluxo encerrado)."""
        conn = await get_db()
        sql = """
            SELECT * FROM rascunhos
            WHERE chat_id = ? AND tarefa_id = ?
            ORDER BY versao DESC
            LIMIT 1
        """
        async with conn.execute(sql, (chat_id, tarefa_id)) as cur:
            row = await cur.fetchone()
        if row is None or bool(row["salvo_no_moodle"]):
            return None
        return _row_to_rascunho(row)

    @staticmethod
    async def marcar_salvo(rascunho_id: int) -> None:
        conn = await get_db()
        await conn.execute(
            "UPDATE rascunhos SET salvo_no_moodle = 1 WHERE id = ?",
            (rascunho_id,),
        )
        await conn.commit()

    @staticmethod
    async def historico(chat_id: int, tarefa_id: int) -> list[Rascunho]:
        conn = await get_db()
        sql = """
            SELECT * FROM rascunhos
            WHERE chat_id = ? AND tarefa_id = ?
            ORDER BY versao ASC
        """
        async with conn.execute(sql, (chat_id, tarefa_id)) as cur:
            rows = await cur.fetchall()
        return [_row_to_rascunho(r) for r in rows]


class ConversaRepo:
    """Estado da máquina de assistência por (chat, tarefa)."""

    @staticmethod
    async def obter(chat_id: int, tarefa_id: int) -> Conversa | None:
        conn = await get_db()
        sql = "SELECT * FROM conversas WHERE chat_id = ? AND tarefa_id = ?"
        async with conn.execute(sql, (chat_id, tarefa_id)) as cur:
            row = await cur.fetchone()
        return _row_to_conversa(row) if row else None

    @staticmethod
    async def upsert_estado(
        chat_id: int,
        tarefa_id: int,
        estado: EstadoConversa,
        contexto: dict | None = None,
    ) -> None:
        """Insere ou atualiza estado.

        Semântica de `contexto`:
        - `None`  → não toca em `contexto_json` (preserva o que existia).
        - dict    → serializa em JSON e sobrescreve.

        Para limpar o contexto explicitamente, passe `{}` (serializado como
        '{}'), não None.
        """
        conn = await get_db()
        contexto_json = json.dumps(contexto, ensure_ascii=False) if contexto is not None else None
        now = _to_iso(datetime.now(timezone.utc))
        sql = """
            INSERT INTO conversas (chat_id, tarefa_id, estado, contexto_json, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, tarefa_id) DO UPDATE SET
                estado = excluded.estado,
                contexto_json = COALESCE(excluded.contexto_json, conversas.contexto_json),
                atualizado_em = excluded.atualizado_em
        """
        await conn.execute(
            sql, (chat_id, tarefa_id, estado.value, contexto_json, now)
        )
        await conn.commit()

    @staticmethod
    async def revisao_ativa_no_chat(chat_id: int) -> Conversa | None:
        """Retorna a Conversa(chat_id, *) em estado=revisando_rascunho mais
        recente. Usado pelo handle_mensagem_refinamento para descobrir o
        alvo do refino sem precisar carregar tarefa_id em cada mensagem."""
        conn = await get_db()
        sql = """
            SELECT * FROM conversas
            WHERE chat_id = ? AND estado = 'revisando_rascunho'
            ORDER BY atualizado_em DESC
            LIMIT 1
        """
        async with conn.execute(sql, (chat_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_conversa(row) if row else None
