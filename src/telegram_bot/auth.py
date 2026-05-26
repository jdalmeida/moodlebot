"""Allowlist de usuários do Telegram.

Política: usuários fora da allowlist são silenciosamente ignorados — não
respondemos sequer "acesso negado" porque revelar a existência do bot já
expõe a conta institucional. Loggamos a tentativa para auditoria.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from ..config import settings


_R = TypeVar("_R")
_Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[_R]]


def is_allowed(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in settings.telegram_allowed_user_ids


def require_allowed_user(handler: _Handler) -> _Handler:
    """Decorator que filtra handlers do telegram.ext pela allowlist."""

    @wraps(handler)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> _R | None:
        user = update.effective_user
        user_id = user.id if user else None
        if not is_allowed(user_id):
            logger.warning(
                "Acesso negado ao bot: user_id={}, username={}",
                user_id,
                user.username if user else None,
            )
            return None
        return await handler(update, context)

    return wrapper  # type: ignore[return-value]
