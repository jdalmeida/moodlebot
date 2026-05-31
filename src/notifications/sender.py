"""Envio de notificações via Telegram + persistência idempotente."""

from __future__ import annotations

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ..config import settings
from ..db import marcar_notificacao_enviada
from ..models import Tarefa, TipoNotif
from ..telegram_bot.formatters import (
    render_notificacao_lembrete,
    render_notificacao_nova,
)


def _keyboard_ajudar(tarefa: Tarefa) -> InlineKeyboardMarkup:
    """Botão único 'Me ajude' que dispara o fluxo assistido para esta tarefa."""
    assert tarefa.id is not None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🤖 Me ajude", callback_data=f"ajudar:{tarefa.id}"
        ),
    ]])


def _render(t: Tarefa, tipo: TipoNotif) -> str:
    match tipo:
        case TipoNotif.NOVA:
            return render_notificacao_nova(t)
        case TipoNotif.LEMBRETE_24H:
            return render_notificacao_lembrete(t, horas=24)
        case TipoNotif.LEMBRETE_2H:
            return render_notificacao_lembrete(t, horas=2)
    raise AssertionError(f"TipoNotif não tratado: {tipo}")


async def enviar_notificacoes(
    bot: Bot, pendentes: list[tuple[Tarefa, TipoNotif]]
) -> int:
    """Envia cada notificação para todos os usuários da allowlist.

    Persistimos UMA linha por (tarefa, tipo) — não por destinatário. Isso
    significa que se um envio para um chat falhar, os demais ainda são
    feitos, mas a notificação é considerada "enviada" globalmente.
    Trade-off intencional: simplicidade > entrega garantida por usuário.

    Retorna o número de pares (tarefa, tipo) efetivamente marcados como
    enviados nesta chamada.
    """
    destinos = settings.telegram_allowed_user_ids
    if not destinos:
        logger.warning("Sem destinatários (TELEGRAM_ALLOWED_USER_IDS vazio).")
        return 0

    enviados = 0
    for tarefa, tipo in pendentes:
        if tarefa.id is None:
            logger.error("Tarefa sem id no banco — não pode notificar: {}", tarefa.titulo)
            continue

        texto = _render(tarefa, tipo)
        keyboard = _keyboard_ajudar(tarefa)
        primeiro_msg_id: int | None = None
        primeiro_chat_id: int | None = None
        sucesso_em_algum = False

        for chat_id in destinos:
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=texto,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
                sucesso_em_algum = True
                if primeiro_msg_id is None:
                    primeiro_msg_id = msg.message_id
                    primeiro_chat_id = chat_id
            except TelegramError as e:
                logger.error(
                    "Falha ao enviar para {}: {}", chat_id, e
                )

        if sucesso_em_algum:
            ok = await marcar_notificacao_enviada(
                tarefa_id=tarefa.id,
                tipo=tipo,
                telegram_chat_id=primeiro_chat_id,
                telegram_message_id=primeiro_msg_id,
            )
            if ok:
                enviados += 1
                logger.info(
                    "Notificação enviada: tipo={} tarefa={!r}",
                    tipo.value,
                    tarefa.titulo,
                )
            else:
                logger.debug(
                    "Notificação já marcada antes (race): tipo={} tarefa_id={}",
                    tipo.value,
                    tarefa.id,
                )

    return enviados
