"""Handlers do bot Telegram — comandos + fluxo de assistência (fase 2).

Concentra:
- `/start`, `/ping`, `/tarefas` (migrados de `bot.py`).
- Callbacks da máquina de assistência:
    callback_ajudar       → estado COLETANDO_CONTEXTO
    callback_rascunhar    → gera v1, estado REVISANDO_RASCUNHO
    callback_salvar       → chama MoodleSubmitter, estado IDLE
    callback_descartar    → estado IDLE
    callback_refazer      → gera nova v1, mantém REVISANDO_RASCUNHO
- Mensagens livres durante REVISANDO_RASCUNHO viram refinamento (Drafter).

Dependências compartilhadas (`Drafter`, `MoodleSession`) chegam via
`context.bot_data`, populado em `src/app.py:_post_init`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from html import escape

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..agent.drafter import Drafter, RascunhoGerado
from ..db import (
    ConversaRepo,
    RascunhoRepo,
    get_tarefa,
    list_tarefas_pendentes,
)
from ..models import EstadoConversa, Rascunho, Tarefa
from ..moodle.submitter import MoodleSubmitter, SubmissaoAcidentalError
from .auth import require_allowed_user
from .formatters import render_lista, render_tarefa


_START_TIME = time.monotonic()

# Telegram aceita até 4096 chars por mensagem. Como o rascunho pode ser
# longo, quebramos em chunks com folga; só o último carrega os botões.
_MAX_CHARS_RASCUNHO = 3600


# --------------------------------------------------------------------------- #
# Comandos básicos (migrados de bot.py)
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
        "<b>🤖 Me ajude</b> para que eu gere um rascunho de resposta."
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
# Helpers de teclado
# --------------------------------------------------------------------------- #


def _keyboard_gerar(tarefa_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶ Gerar rascunho", callback_data=f"rascunhar:{tarefa_id}"
            ),
            InlineKeyboardButton(
                "✖ Cancelar", callback_data=f"descartar:{tarefa_id}"
            ),
        ],
    ])


def _keyboard_revisao(tarefa_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💾 Salvar", callback_data=f"salvar:{tarefa_id}"
            ),
            InlineKeyboardButton(
                "🔄 Refazer", callback_data=f"refazer:{tarefa_id}"
            ),
            InlineKeyboardButton(
                "🗑 Descartar", callback_data=f"descartar:{tarefa_id}"
            ),
        ],
    ])


def _contexto_para_drafter(tarefa: Tarefa) -> dict:
    """Serializa a Tarefa em dict para guardar como `contexto_json` da Conversa
    e/ou alimentar o Drafter. Mantém só campos que ajudam o LLM."""
    contexto: dict = {
        "titulo": tarefa.titulo,
        "curso": tarefa.curso,
        "tipo": tarefa.tipo.value,
        "url": str(tarefa.url),
    }
    if tarefa.prazo:
        contexto["prazo_iso"] = tarefa.prazo.isoformat()
    if tarefa.descricao:
        contexto["descricao"] = tarefa.descricao
    return contexto


def _quebrar_em_chunks(texto: str, max_chars: int) -> list[str]:
    """Quebra um texto longo em pedaços <= max_chars, preferindo quebra em
    parágrafo. Usado para rascunhos que excedem o limite do Telegram."""
    if len(texto) <= max_chars:
        return [texto]
    chunks: list[str] = []
    restante = texto
    while len(restante) > max_chars:
        # Tenta quebrar no último \n\n dentro da janela; senão no último \n;
        # senão corta seco.
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


async def _enviar_rascunho(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tarefa_id: int,
    rascunho: Rascunho,
) -> None:
    """Envia o rascunho ao chat (quebrando se necessário) e anexa os botões
    de revisão à ÚLTIMA mensagem."""
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id

    header = f"📝 <b>Rascunho v{rascunho.versao}</b>"
    await context.bot.send_message(
        chat_id=chat_id, text=header, parse_mode=ParseMode.HTML
    )

    chunks = _quebrar_em_chunks(rascunho.conteudo, _MAX_CHARS_RASCUNHO)
    keyboard = _keyboard_revisao(tarefa_id)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await context.bot.send_message(
            chat_id=chat_id,
            text=escape(chunk),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard if is_last else None,
        )


def _parse_tarefa_id(callback_data: str | None) -> int | None:
    """Extrai o tarefa_id de strings como 'ajudar:42'. None se inválido."""
    if not callback_data or ":" not in callback_data:
        return None
    try:
        return int(callback_data.split(":", 1)[1])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Callbacks — máquina de assistência
# --------------------------------------------------------------------------- #


@require_allowed_user
async def callback_ajudar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entrada do fluxo: 'Me ajude' numa notificação."""
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

    contexto = _contexto_para_drafter(tarefa)
    await ConversaRepo.upsert_estado(
        chat_id=chat_id,
        tarefa_id=tarefa_id,
        estado=EstadoConversa.COLETANDO_CONTEXTO,
        contexto=contexto,
    )

    resumo = (
        "🤔 <b>Vou preparar um rascunho para esta tarefa.</b>\n\n"
        + render_tarefa(tarefa)
        + "\n\nClique abaixo quando quiser que eu gere a primeira versão. "
        "Depois você pode mandar mensagens para refinar."
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=resumo,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_keyboard_gerar(tarefa_id),
    )
    await query.answer()


@require_allowed_user
async def callback_rascunhar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gera a primeira versão do rascunho via Drafter."""
    query = update.callback_query
    assert query is not None
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id

    tarefa_id = _parse_tarefa_id(query.data)
    if tarefa_id is None:
        await query.answer("Botão inválido.", show_alert=True)
        return

    tarefa = await get_tarefa(tarefa_id)
    conversa = await ConversaRepo.obter(chat_id, tarefa_id)
    if tarefa is None or conversa is None:
        await query.answer("Sessão expirou. Toque 'Me ajude' de novo.", show_alert=True)
        return

    drafter: Drafter = context.bot_data["drafter"]
    contexto_dict = json.loads(conversa.contexto_json) if conversa.contexto_json else None

    await query.answer("Gerando…")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        resultado: RascunhoGerado = await drafter.gerar_rascunho(tarefa, contexto_dict)
    except Exception as e:  # noqa: BLE001
        logger.exception("Falha ao gerar rascunho")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Falhou ao gerar rascunho: {e}"
        )
        return

    rascunho = await RascunhoRepo.criar(
        tarefa_id=tarefa_id,
        chat_id=chat_id,
        conteudo=resultado.conteudo,
        prompt_usado=resultado.prompt,
    )
    await ConversaRepo.upsert_estado(
        chat_id=chat_id,
        tarefa_id=tarefa_id,
        estado=EstadoConversa.REVISANDO_RASCUNHO,
    )
    await _enviar_rascunho(update, context, tarefa_id, rascunho)


@require_allowed_user
async def handle_mensagem_refinamento(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Mensagem de texto livre durante REVISANDO_RASCUNHO = pedido de refino."""
    assert update.effective_chat is not None
    assert update.message is not None
    chat_id = update.effective_chat.id
    texto_usuario = (update.message.text or "").strip()
    if not texto_usuario:
        return

    conversa = await ConversaRepo.revisao_ativa_no_chat(chat_id)
    if conversa is None:
        # Não há rascunho em revisão — responde uma vez, sem persistir nada.
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Não há rascunho ativo para refinar. "
                "Use /tarefas e clique em <b>🤖 Me ajude</b> em uma notificação primeiro."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    tarefa = await get_tarefa(conversa.tarefa_id)
    anterior = await RascunhoRepo.ativo_para(chat_id, conversa.tarefa_id)
    if tarefa is None or anterior is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Estado inconsistente — descarte e comece de novo.",
        )
        return

    drafter: Drafter = context.bot_data["drafter"]
    contexto_dict = json.loads(conversa.contexto_json) if conversa.contexto_json else None

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        resultado = await drafter.refinar_rascunho(
            anterior=anterior.conteudo,
            instrucao=texto_usuario,
            tarefa=tarefa,
            contexto=contexto_dict,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Falha ao refinar rascunho")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Falhou ao refinar: {e}"
        )
        return

    rascunho = await RascunhoRepo.criar(
        tarefa_id=conversa.tarefa_id,
        chat_id=chat_id,
        conteudo=resultado.conteudo,
        prompt_usado=resultado.prompt,
    )
    await _enviar_rascunho(update, context, conversa.tarefa_id, rascunho)


@require_allowed_user
async def callback_salvar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persiste o rascunho ativo no Moodle (como rascunho, NÃO submete)."""
    query = update.callback_query
    assert query is not None
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id

    tarefa_id = _parse_tarefa_id(query.data)
    if tarefa_id is None:
        await query.answer("Botão inválido.", show_alert=True)
        return

    tarefa = await get_tarefa(tarefa_id)
    rascunho = await RascunhoRepo.ativo_para(chat_id, tarefa_id)
    if tarefa is None or rascunho is None:
        await query.answer("Sem rascunho ativo.", show_alert=True)
        return

    await query.answer("Salvando no Moodle…")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    submitter = MoodleSubmitter()
    try:
        await submitter.salvar_rascunho(str(tarefa.url), rascunho.conteudo)
    except SubmissaoAcidentalError as e:
        logger.error("SUBMISSÃO ACIDENTAL detectada: {}", e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🚨 <b>ALERTA</b>: o submitter detectou que a tarefa pode "
                "ter sido <b>enviada para avaliação</b> em vez de salva como "
                f"rascunho.\n\nDetalhe: <code>{escape(str(e))}</code>\n\n"
                "Verifique no Moodle antes de prosseguir."
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("Falha ao salvar rascunho no Moodle")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Falhou ao salvar no Moodle: {escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return

    assert rascunho.id is not None
    await RascunhoRepo.marcar_salvo(rascunho.id)
    await ConversaRepo.upsert_estado(
        chat_id=chat_id, tarefa_id=tarefa_id, estado=EstadoConversa.IDLE
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Rascunho v{rascunho.versao} salvo no Moodle "
            "(<i>não enviado para avaliação</i>)."
        ),
        parse_mode=ParseMode.HTML,
    )


@require_allowed_user
async def callback_descartar(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Sai da assistência sem salvar nada no Moodle."""
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
    await query.answer("Descartado.")
    try:
        # Remove os botões da mensagem origem para deixar o estado visível.
        if query.message is not None:
            await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


@require_allowed_user
async def callback_refazer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gera uma nova v1 do zero (sem usar o rascunho anterior como base)."""
    query = update.callback_query
    assert query is not None
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id

    tarefa_id = _parse_tarefa_id(query.data)
    if tarefa_id is None:
        await query.answer("Botão inválido.", show_alert=True)
        return

    tarefa = await get_tarefa(tarefa_id)
    conversa = await ConversaRepo.obter(chat_id, tarefa_id)
    if tarefa is None or conversa is None:
        await query.answer("Sessão expirou.", show_alert=True)
        return

    drafter: Drafter = context.bot_data["drafter"]
    contexto_dict = json.loads(conversa.contexto_json) if conversa.contexto_json else None

    await query.answer("Refazendo…")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        resultado = await drafter.gerar_rascunho(tarefa, contexto_dict)
    except Exception as e:  # noqa: BLE001
        logger.exception("Falha ao refazer rascunho")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Falhou ao refazer: {e}"
        )
        return

    rascunho = await RascunhoRepo.criar(
        tarefa_id=tarefa_id,
        chat_id=chat_id,
        conteudo=resultado.conteudo,
        prompt_usado=resultado.prompt,
    )
    await _enviar_rascunho(update, context, tarefa_id, rascunho)
