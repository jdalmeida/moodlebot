"""Configuração do loguru.

Chamar `setup_logging()` uma única vez no boot do processo.
"""

from __future__ import annotations

import sys

from loguru import logger

from .config import settings


_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def setup_logging() -> None:
    """Reconfigura o logger global do loguru segundo `settings.log_level`."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        format=_FORMAT,
        backtrace=True,
        diagnose=False,  # evita vazar valores sensíveis nos tracebacks
    )
