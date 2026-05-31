"""Handlers do bot Telegram — comandos + fluxo de orientação.

Concentra:
- `/start`, `/ping`, `/tarefas`.
- Callbacks do fluxo de orientação:
    callback_orientar   → roda o Orientador (agente), envia o roteiro
    callback_fechar     → encerra (estado IDLE), remove o teclado

O fluxo antigo de rascunho (gerar/refinar/salvar resposta pronta) foi
substituído pela orientação: o agente lê o Moodle e devolve um ROTEIRO de como
resolver, em vez da resposta pronta. O `Orientador` chega via `context.bot_data`,
populado em `src/app.py:_post_init`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..agent.orientador import Orientador
from ..db import (
    ConversaRepo,
    get_tarefa,
    list_tarefas_pendentes,
)
from ..models import EstadoConversa
from .auth import require_allowed_user
from .formatters import render_lista


_START_TIME = time.monotonic()

# Telegram aceita até 4096 chars por mensagem. Quebramos com folga; só a última
# mensagem carrega os botões.
_MAX_CHARS_ROTEIRO = 3600


# --------------------------------------------------------------------------- #
# Comandos básicos
# --------------------------------------------------------------------------- #


@require_allowed_user
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_chat is not None
    msg = (
        "👋 <b>Moodlebot</b> — monitor de tarefas do Moodle\n\n"
        "Comandos disponíveis:\n"
        "• /ping — verifica se estou vivo\n"
        "• /tarefas — lista tarefas pendentes\n\n"
        "Quando eu mandar uma notificação de tarefa, use o botão "
        "<b>🧭 Orientar</b>: eu leio o enunciado e os materiais no Moodle e "
        "monto um roteiro de como resolver (não a resposta pronta)."
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _keyboard_orientacao(tarefa_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 Re-orientar", callback_data=f"reorientar:{tarefa_id}"
            ),
            InlineKeyboardButton(
                "✖ Fechar", callback_data=f"fechar:{tarefa_id}"
            ),
        ],
    ])


def _quebrar_em_chunks(texto: str, max_chars: int) -> list[str]:
    """Quebra um texto longo em pedaços <= max_chars, preferindo quebra em
    parágrafo. Usado para roteiros que excedem o limite do Telegram."""
    if len(texto) <= max_chars:
        return [texto]
    chunks: list[str] = []
    restante = texto
    while len(restante) > max_chars:
        corte = restante.rfind("\n\n", 0, max_chars)
        if corte == -1:
            corte = restante.rfind("\n", 0, max_chars)
        if corte == -1:
            corte = max_chars
        chunks.append(restante[:corte].rstrip())
        restante = restante[corte:].lstrip()
    if restante:
        chunks.append(restante)
    return chunks


def _parse_tarefa_id(callback_data: str | None) -> int | None:
    """Extrai o tarefa_id de strings como 'orientar:42'. None se inválido."""
    if not callback_data or ":" not in callback_data:
        return None
    try:
        return int(callback_data.split(":", 1)[1])
    except ValueError:
        return None


async def _enviar_roteiro(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    tarefa_id: int,
    roteiro: str,
) -> None:
    """Envia o roteiro (quebrando se necessário) com os botões na última msg.

    O roteiro vem em markdown do modelo; enviamos como texto puro (sem
    parse_mode) para não quebrar com caracteres especiais.
    """
    header = "🧭 <b>Orientação</b>"
    await context.bot.send_message(
        chat_id=chat_id, text=header, parse_mode=ParseMode.HTML
    )
    chunks = _quebrar_em_chunks(roteiro or "(sem conteúdo)", _MAX_CHARS_ROTEIRO)
    keyboard = _keyboard_orientacao(tarefa_id)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            disable_web_page_preview=True,
            reply_markup=keyboard if is_last else None,
        )


# --------------------------------------------------------------------------- #
# Callbacks — fluxo de orientação
# --------------------------------------------------------------------------- #


@require_allowed_user
async def callback_orientar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Roda o Orientador para a tarefa e envia o roteiro. Atende tanto
    'orientar:<id>' quanto 'reorientar:<id>'."""
    query = update.callback_query
    assert query is not None
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id

    tarefa_id = _parse_tarefa_id(query.data)
    if tarefa_id is None:
        await query.answer("Botão inválido.", show_alert=True)
        return

    tarefa = await get_tarefa(tarefa_id)
    if tarefa is None:
        await query.answer("Tarefa não encontrada no banco.", show_alert=True)
        return

    await query.answer("Analisando a tarefa… pode levar ~1 min.")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    orientador: Orientador = context.bot_data["orientador"]
    try:
        orientacao = await orientador.orientar(tarefa)
    except Exception as e:  # noqa: BLE001
        logger.exception("Falha ao orientar")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Falhou ao gerar orientação: {e}"
        )
        return

    await ConversaRepo.upsert_estado(
        chat_id=chat_id,
        tarefa_id=tarefa_id,
        estado=EstadoConversa.REVISANDO_ORIENTACAO,
        contexto={"orientacao": orientacao.roteiro, "contexto": orientacao.contexto},
    )
    await _enviar_roteiro(context, chat_id, tarefa_id, orientacao.roteiro)


@require_allowed_user
async def callback_fechar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Encerra a orientação: estado IDLE e remove o teclado da mensagem."""
    query = update.callback_query
    assert query is not None
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id

    tarefa_id = _parse_tarefa_id(query.data)
    if tarefa_id is None:
        await query.answer("Botão inválido.", show_alert=True)
        return

    await ConversaRepo.upsert_estado(
        chat_id=chat_id, tarefa_id=tarefa_id, estado=EstadoConversa.IDLE
    )
    await query.answer("Fechado.")
    try:
        if query.message is not None:
            await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
