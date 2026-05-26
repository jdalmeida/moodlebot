"""Política de notificação.

Define quando cada tipo de notificação deve disparar e devolve pares
(Tarefa, TipoNotif) prontos para serem enviados pelo `sender`.

Idempotência: as próprias queries fazem anti-join com `notificacoes_enviadas`
para nunca devolver algo que já tenha sido notificado.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from ..db import (
    tarefas_pendentes_sem_notif_nova,
    tarefas_precisando_lembrete,
)
from ..models import Tarefa, TipoNotif


# Janelas largas porque o poll roda a cada N minutos. Se a tarefa cai na
# janela em algum tick, ela é apanhada — mais robusto que esperar bater
# o instante exato.
_JANELA_24H_INICIO_HORAS = 22
_JANELA_24H_FIM_HORAS = 26
_JANELA_2H_INICIO_HORAS = 1
_JANELA_2H_FIM_HORAS = 3


async def calcular_notificacoes_pendentes(
    *, now: datetime | None = None
) -> list[tuple[Tarefa, TipoNotif]]:
    """Retorna pares (tarefa, tipo) que ainda não foram notificados.

    O caller (sender) decide se envia tudo de uma vez ou em lote.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("`now` deve ser timezone-aware")

    pendentes: list[tuple[Tarefa, TipoNotif]] = []

    # NOVA
    for t in await tarefas_pendentes_sem_notif_nova():
        pendentes.append((t, TipoNotif.NOVA))

    # LEMBRETE_24H
    janela_24h = (
        now + timedelta(hours=_JANELA_24H_INICIO_HORAS),
        now + timedelta(hours=_JANELA_24H_FIM_HORAS),
    )
    for t in await tarefas_precisando_lembrete(
        TipoNotif.LEMBRETE_24H, *janela_24h
    ):
        pendentes.append((t, TipoNotif.LEMBRETE_24H))

    # LEMBRETE_2H
    janela_2h = (
        now + timedelta(hours=_JANELA_2H_INICIO_HORAS),
        now + timedelta(hours=_JANELA_2H_FIM_HORAS),
    )
    for t in await tarefas_precisando_lembrete(
        TipoNotif.LEMBRETE_2H, *janela_2h
    ):
        pendentes.append((t, TipoNotif.LEMBRETE_2H))

    if pendentes:
        logger.info(
            "{} notificação(ões) pendente(s) a enviar", len(pendentes)
        )
    return pendentes
