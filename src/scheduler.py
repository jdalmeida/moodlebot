"""Scheduler — orquestra o tick periódico de coleta + notificação."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from telegram import Bot
from telegram.error import TelegramError

from .config import settings
from .db import upsert_tarefa
from .models import Tarefa
from .moodle import reader
from .moodle.scraper import coletar_tarefas_pendentes
from .moodle.session import MoodleSession, SessionExpiredError
from .notifications.policy import calcular_notificacoes_pendentes
from .notifications.sender import enviar_notificacoes


# Lock global: garante que dois ticks não rodem em paralelo se o anterior
# demorou mais que o intervalo de poll. APScheduler tem max_instances=1 mas
# o lock fornece defesa em profundidade — e ainda permite chamar tick()
# manualmente durante debug sem sobrepor com o agendado.
_tick_lock = asyncio.Lock()


async def tick(session: MoodleSession, bot: Bot) -> None:
    """Um ciclo: coleta tarefas, faz upsert, envia notificações pendentes."""
    if _tick_lock.locked():
        logger.debug("Tick anterior ainda em execução — pulando este.")
        return

    async with _tick_lock:
        logger.info("=== Tick iniciado ===")
        try:
            await session.ensure_logged_in()
        except SessionExpiredError as e:
            logger.error("Sessão Moodle expirada: {}", e)
            await _avisar_sessao_expirada(bot)
            return

        try:
            tarefas = await coletar_tarefas_pendentes(session)
        except Exception as e:  # noqa: BLE001
            logger.exception("Falha ao coletar tarefas: {}", e)
            return

        logger.info("Coletadas {} tarefa(s) do dashboard", len(tarefas))

        novas = 0
        recem_criadas: list[Tarefa] = []
        for t in tarefas:
            try:
                _, recem_criada = await upsert_tarefa(t)
                if recem_criada:
                    novas += 1
                    recem_criadas.append(t)
            except Exception as e:  # noqa: BLE001
                logger.error("Falha ao salvar tarefa {!r}: {}", t.moodle_id, e)

        logger.info("UPSERT concluído ({} nova(s))", novas)

        # Coleta híbrida: para tarefas novas sem enunciado conhecido, abre a
        # página da atividade e grava o enunciado completo (sem baixar anexos —
        # download pesado fica para a orientação sob demanda).
        await _enriquecer_enunciados(session, recem_criadas)

        try:
            now = datetime.now(timezone.utc)
            pendentes = await calcular_notificacoes_pendentes(now=now)
            await enviar_notificacoes(bot, pendentes)
        except Exception as e:  # noqa: BLE001
            logger.exception("Falha ao enviar notificações: {}", e)


async def _enriquecer_enunciados(
    session: MoodleSession, tarefas: list[Tarefa]
) -> None:
    """Para cada tarefa nova sem descrição, lê o enunciado da atividade e
    persiste em `tarefas.descricao`. Best-effort: falhas só logam."""
    for t in tarefas:
        if t.descricao:
            continue
        try:
            det = await reader.obter_atividade(session, str(t.url), t.tipo)
        except Exception as e:  # noqa: BLE001
            logger.warning("Não consegui ler enunciado de {!r}: {}", t.titulo, e)
            continue
        if det.enunciado_texto:
            t.descricao = det.enunciado_texto
            try:
                await upsert_tarefa(t)
            except Exception as e:  # noqa: BLE001
                logger.error("Falha ao gravar enunciado de {!r}: {}", t.moodle_id, e)


# Estado para não fazer spam do aviso de sessão expirada a cada tick.
_aviso_sessao_enviado: bool = False


async def _avisar_sessao_expirada(bot: Bot) -> None:
    global _aviso_sessao_enviado
    if _aviso_sessao_enviado:
        return
    texto = (
        "⚠️ <b>Sessão Moodle expirou</b>\n\n"
        "Rode novamente:\n"
        "<code>python scripts/first_login.py</code>"
    )
    for chat_id in settings.telegram_allowed_user_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=texto, parse_mode="HTML")
        except TelegramError as e:
            logger.error("Não consegui avisar sobre sessão expirada: {}", e)
    _aviso_sessao_enviado = True


def reset_aviso_sessao() -> None:
    """Chamar após um relogin bem-sucedido."""
    global _aviso_sessao_enviado
    _aviso_sessao_enviado = False


def build_scheduler(session: MoodleSession, bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes),
        kwargs={"session": session, "bot": bot},
        id="moodlebot-tick",
        name="Coleta + notificações",
        max_instances=1,
        coalesce=True,        # se o agendador atrasou, executa só uma vez
        next_run_time=datetime.now(timezone.utc),  # roda imediatamente no boot
    )
    return scheduler
