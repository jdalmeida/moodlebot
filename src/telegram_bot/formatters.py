"""Renderização de objetos do domínio para texto Telegram (parse_mode=HTML)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from ..models import Tarefa


# Telegram limita mensagens a 4096 chars. Deixamos folga para evitar bordas.
_MAX_CHARS = 3800


def _fmt_prazo(prazo: datetime | None) -> str:
    if prazo is None:
        return "sem prazo"
    # Mostra em BRT (UTC-3) — o usuário lê em horário local.
    local = prazo.astimezone(timezone.utc).astimezone()
    return local.strftime("%d/%m/%Y %H:%M")


def render_tarefa(t: Tarefa) -> str:
    """Bloco curto para uma tarefa, formatado em HTML do Telegram."""
    titulo = escape(t.titulo)
    curso = escape(t.curso)
    prazo = escape(_fmt_prazo(t.prazo))
    tipo = escape(t.tipo.value)
    return (
        f"📚 <b>{titulo}</b>\n"
        f"🎓 {curso}\n"
        f"🏷️ {tipo} • ⏰ {prazo}\n"
        f"🔗 <a href=\"{escape(str(t.url))}\">abrir no Moodle</a>"
    )


def render_lista(tarefas: list[Tarefa]) -> list[str]:
    """Concatena `render_tarefa` em mensagens ≤ _MAX_CHARS.

    Retorna uma lista — o caller deve enviar uma de cada vez.
    """
    if not tarefas:
        return ["Nenhuma tarefa pendente conhecida no momento. 🎉"]

    header = f"<b>Tarefas pendentes ({len(tarefas)})</b>\n\n"
    chunks: list[str] = []
    atual = header
    for t in tarefas:
        bloco = render_tarefa(t) + "\n\n"
        if len(atual) + len(bloco) > _MAX_CHARS:
            chunks.append(atual.rstrip())
            atual = bloco
        else:
            atual += bloco
    if atual.strip():
        chunks.append(atual.rstrip())
    return chunks


def render_notificacao_nova(t: Tarefa) -> str:
    return "🆕 <b>Nova tarefa detectada</b>\n\n" + render_tarefa(t)


def render_notificacao_lembrete(t: Tarefa, horas: int) -> str:
    emoji = "⏳" if horas >= 6 else "🚨"
    titulo = f"{emoji} <b>Prazo em ~{horas}h</b>"
    return f"{titulo}\n\n" + render_tarefa(t)
