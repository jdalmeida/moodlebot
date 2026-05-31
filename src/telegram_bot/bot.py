"""Construção da `Application` Telegram.

Decisão: usamos long polling (`run_polling`) em vez de webhook. Sem expor
endpoint HTTP, o bot pode rodar atrás de NAT em qualquer máquina (inclusive
no notebook da universidade).

Os handlers vivem em `handlers.py` — este módulo é só o factory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from ..config import settings
from .handlers import (
    callback_fechar,
    callback_orientar,
    cmd_ping,
    cmd_start,
    cmd_tarefas,
)


PostHook = Callable[[Application], Awaitable[None]]


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

    # Comandos slash
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("tarefas", cmd_tarefas))

    # Callbacks do fluxo de orientação. Pattern restrito a `^<verbo>:\d+$`
    # impede colisão com mensagens espúrias e bloqueia callback_data malformado.
    # 'orientar' (da notificação) e 'reorientar' (botão) caem no mesmo handler.
    app.add_handler(
        CallbackQueryHandler(callback_orientar, pattern=r"^(orientar|reorientar):\d+$")
    )
    app.add_handler(CallbackQueryHandler(callback_fechar, pattern=r"^fechar:\d+$"))

    return app
