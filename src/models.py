"""Modelos pydantic do domínio.

Espelham as tabelas SQLite mas vivem isolados delas — útil para que outras
camadas (LLM, Telegram) recebam objetos validados sem acoplar ao schema.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class TipoTarefa(StrEnum):
    """Módulo Moodle de origem da tarefa.

    OTHER é o catch-all para módulos não mapeados — mantém o pipeline rodando
    em vez de falhar quando um curso usa algo exótico (workshop, h5pactivity…).
    """

    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    FORUM = "forum"
    CHOICE = "choice"
    WORKSHOP = "workshop"
    OTHER = "other"


class StatusTarefa(StrEnum):
    PENDENTE = "pendente"
    ENTREGUE = "entregue"
    ARQUIVADA = "arquivada"


class TipoNotif(StrEnum):
    NOVA = "nova"
    LEMBRETE_24H = "lembrete_24h"
    LEMBRETE_2H = "lembrete_2h"


class EstadoConversa(StrEnum):
    """Estados da máquina de conversação por (chat_id, tarefa_id)."""

    IDLE = "idle"
    COLETANDO_CONTEXTO = "coletando_contexto"
    REVISANDO_RASCUNHO = "revisando_rascunho"


class Tarefa(BaseModel):
    """Tarefa pendente de um curso Moodle."""

    model_config = ConfigDict(use_enum_values=False, str_strip_whitespace=True)

    id: int | None = None
    moodle_id: str = Field(
        description="Identificador externo (cmid do Moodle). Único globalmente."
    )
    tipo: TipoTarefa = TipoTarefa.OTHER
    curso: str
    titulo: str
    url: HttpUrl
    prazo: datetime | None = Field(
        default=None,
        description="Datetime aware UTC. None se a tarefa não expõe prazo.",
    )
    descricao: str | None = None
    status: StatusTarefa = StatusTarefa.PENDENTE
    primeira_visualizacao: datetime | None = None
    atualizado_em: datetime | None = None


class NotificacaoEnviada(BaseModel):
    id: int | None = None
    tarefa_id: int
    tipo_notif: TipoNotif
    enviado_em: datetime
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None


class Rascunho(BaseModel):
    """Versão de um rascunho gerado pelo LLM para uma tarefa."""

    id: int | None = None
    tarefa_id: int
    chat_id: int
    versao: int
    conteudo: str
    prompt_usado: str | None = None
    salvo_no_moodle: bool = False
    criado_em: datetime | None = None


class Conversa(BaseModel):
    """Estado vivo da assistência por (chat_id, tarefa_id)."""

    chat_id: int
    tarefa_id: int
    estado: EstadoConversa = EstadoConversa.IDLE
    contexto_json: str | None = None
    atualizado_em: datetime | None = None
