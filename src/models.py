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


class StatusRascunho(StrEnum):
    GERADO = "gerado"
    APROVADO = "aprovado"
    REJEITADO = "rejeitado"
    PREENCHIDO = "preenchido"


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
    id: int | None = None
    tarefa_id: int
    conteudo: str
    status: StatusRascunho = StatusRascunho.GERADO
    modelo_usado: str | None = None
    prompt_usado: str | None = None
    gerado_em: datetime | None = None
    aprovado_em: datetime | None = None
    preenchido_em: datetime | None = None
