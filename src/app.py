"""Orquestrador async: amarra DB + Moodle + Telegram + Scheduler.

Decisão de design: a `Application` do python-telegram-bot já administra um
event loop (via `run_polling`). Em vez de criar um loop separado para o
APScheduler, plugamos um `AsyncIOScheduler` que reusa o mesmo loop. Isso
elimina toda complicação de threads e sincronização.

Sequência de boot:
    1. setup logging
    2. init_schema (idempotente)
    3. abrir sessão Moodle (já com storage_state)
    4. construir Application Telegram + scheduler
    5. agendar `scheduler.start()` para acontecer DEPOIS do bot estar pronto
       (via `post_init`)
    6. `app.run_polling()` bloqueia até SIGINT/SIGTERM
    7. cleanup
"""

from __future__ import annotations

import asyncio

from loguru import logger
from telegram.ext import Application

from .agent.drafter import Drafter
from .config import settings
from .db import close_db, init_schema
from .logging_setup import setup_logging
from .moodle.session import MoodleSession, SessionExpiredError
from .scheduler import build_scheduler


async def _post_init(app: Application) -> None:
    """Chamado pelo telegram.ext após `app.initialize()` e antes do polling."""
    # Banco
    await init_schema()

    # Sessão Moodle
    session = MoodleSession()
    try:
        await session.start()
    except SessionExpiredError as e:
        logger.error("{}", e)
        logger.error("O agente continuará rodando, mas a coleta vai falhar até o login.")
        # Continuamos mesmo assim — o bot ainda responde a /ping e /tarefas
        # com o que já está no banco. O scheduler vai logar erro a cada tick
        # e enviar aviso ao usuário pelo Telegram.

    scheduler = build_scheduler(session, app.bot)
    scheduler.start()
    logger.info(
        "Scheduler iniciado (intervalo: {} min)", settings.poll_interval_minutes
    )

    # Drafter (Gemini). Erra cedo se GOOGLE_API_KEY não estiver configurado.
    drafter = Drafter(api_key=settings.google_api_key, model=settings.llm_model)

    # Guardar referências no bot_data para handlers e shutdown limpo.
    app.bot_data["moodle_session"] = session
    app.bot_data["scheduler"] = scheduler
    app.bot_data["drafter"] = drafter


async def _post_shutdown(app: Application) -> None:
    scheduler = app.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)

    session: MoodleSession | None = app.bot_data.get("moodle_session")
    if session is not None:
        await session.stop()

    await close_db()
    logger.info("Moodlebot encerrado")


def run() -> None:
    """Ponto de entrada síncrono — `run_polling` administra o loop."""
    setup_logging()
    logger.info("Iniciando Moodlebot… (modo: {})", settings.run_mode)

    if settings.run_mode == "terminal":
        # Import lazy (mesma razão do build_application abaixo): mantém o
        # setup_logging rodando antes de qualquer import pesado.
        from .terminal.runner import run_terminal

        asyncio.run(run_terminal())
        return

    # Importa aqui (em vez de no topo) para que `setup_logging` rode antes,
    # garantindo que logs do PTB respeitem o nível configurado.
    from .telegram_bot.bot import build_application

    app = build_application(post_init=_post_init, post_shutdown=_post_shutdown)

    # Python 3.14 mudou: `asyncio.get_event_loop()` não cria mais um loop
    # implicitamente quando chamado fora de uma corrotina — lança RuntimeError.
    # PTB 21 ainda chama essa API em `run_polling`. Garantimos manualmente
    # que existe um loop atribuído à thread principal.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # `run_polling` administra o asyncio loop, captura SIGINT/SIGTERM e
    # chama `post_shutdown` antes de sair.
    app.run_polling(close_loop=True)
