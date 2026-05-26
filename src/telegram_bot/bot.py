"""Construção do bot Telegram.

Decisão: usamos long polling (`run_polling`) em vez de webhook. Sem expor
endpoint HTTP, o bot pode rodar atrás de NAT em qualquer máquina (inclusive
no notebook da universidade).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from ..config import settings
from ..db import list_tarefas_pendentes
from .auth import require_allowed_user
from .formatters import render_lista


PostHook = Callable[[Application], Awaitable[None]]


_START_TIME = time.monotonic()


@require_allowed_user
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_chat is not None
    msg = (
        "👋 <b>Moodlebot</b> — monitor de tarefas do Moodle\n\n"
        "Comandos disponíveis:\n"
        "• /ping — verifica se estou vivo\n"
        "• /tarefas — lista tarefas pendentes\n"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=msg, parse_mode=ParseMode.HTML
    )


@require_allowed_user
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_chat is not None
    uptime_s = int(time.monotonic() - _START_TIME)
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"pong\nuptime: {uptime_s}s\nagora: {now}",
    )


@require_allowed_user
async def cmd_tarefas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    tarefas = await list_tarefas_pendentes()
    for chunk in render_lista(tarefas):
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


def build_application(
    *,
    post_init: PostHook | None = None,
    post_shutdown: PostHook | None = None,
) -> Application:
    """Cria a `Application` pronta para `run_polling()`."""
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado no .env.")
    if not settings.telegram_allowed_user_ids:
        logger.warning(
            "TELEGRAM_ALLOWED_USER_IDS está vazio — ninguém vai conseguir falar com o bot."
        )

    builder = ApplicationBuilder().token(settings.telegram_bot_token)
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_shutdown is not None:
        builder = builder.post_shutdown(post_shutdown)
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("tarefas", cmd_tarefas))
    return app
