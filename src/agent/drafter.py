"""Geração e refinamento de rascunhos via Google Gemini.

Stateless: cada chamada recebe todo o contexto. O histórico de versões vive
em `rascunhos` (RascunhoRepo), não em memória.

SDK usada: `google-genai` (nova oficial; substitui `google-generativeai`).
Models preview do Gemini 3.x só são suportados nessa SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google import genai
from google.genai import types
from loguru import logger

from ..models import Tarefa


SYSTEM_PROMPT = """\
Você é um assistente acadêmico que ajuda um estudante universitário a redigir \
respostas para tarefas do Moodle.

Diretrizes:
- Responda DIRETAMENTE em português brasileiro, sem preâmbulos do tipo \
"Aqui está sua resposta".
- Tom claro e correto, no nível esperado para a disciplina informada.
- Quando faltar informação, escreva o melhor esforço com base no enunciado — \
não invente fatos específicos (nomes, citações, dados numéricos).
- Não inclua assinatura, data, cabeçalho, nem comentários sobre o próprio \
rascunho.
- Adeque o tamanho ao tipo da tarefa (resposta curta para fórum; mais \
elaborada para trabalho escrito).
"""


@dataclass(frozen=True, slots=True)
class RascunhoGerado:
    """Resultado de uma chamada ao Drafter."""

    conteudo: str
    prompt: str


class Drafter:
    """Gera e refina rascunhos via Gemini."""

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY não configurado — o Drafter não pode iniciar."
            )
        self._client = genai.Client(api_key=api_key)
        self._model = model
        logger.info("Drafter pronto: model={}", model)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def gerar_rascunho(
        self, tarefa: Tarefa, contexto: dict | None = None
    ) -> RascunhoGerado:
        """Primeira versão do rascunho."""
        prompt = self._montar_prompt_geracao(tarefa, contexto)
        texto = await self._call(prompt)
        return RascunhoGerado(conteudo=texto, prompt=prompt)

    async def refinar_rascunho(
        self,
        anterior: str,
        instrucao: str,
        tarefa: Tarefa,
        contexto: dict | None = None,
    ) -> RascunhoGerado:
        """Nova versão a partir do rascunho anterior + instrução do usuário."""
        prompt = self._montar_prompt_refinamento(
            anterior=anterior, instrucao=instrucao, tarefa=tarefa, contexto=contexto
        )
        texto = await self._call(prompt)
        return RascunhoGerado(conteudo=texto, prompt=prompt)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _call(self, prompt: str) -> str:
        """Chama o modelo e devolve o texto, com tratamento defensivo."""
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
            ),
        )
        texto = (response.text or "").strip()
        if not texto:
            raise RuntimeError(
                f"Modelo {self._model} devolveu resposta vazia. "
                f"Veja o `response`: {response!r}"
            )
        return texto

    @staticmethod
    def _bloco_tarefa(tarefa: Tarefa, contexto: dict | None) -> str:
        partes = [
            f"TAREFA: {tarefa.titulo}",
            f"CURSO: {tarefa.curso}",
            f"TIPO: {tarefa.tipo.value}",
        ]
        if tarefa.prazo:
            partes.append(f"PRAZO: {tarefa.prazo.isoformat()}")
        if tarefa.descricao:
            partes.append(f"DESCRIÇÃO/ENUNCIADO:\n{tarefa.descricao}")
        if contexto:
            partes.append(
                "CONTEXTO ADICIONAL (JSON):\n"
                + json.dumps(contexto, ensure_ascii=False, indent=2)
            )
        return "\n".join(partes)

    def _montar_prompt_geracao(
        self, tarefa: Tarefa, contexto: dict | None
    ) -> str:
        return (
            self._bloco_tarefa(tarefa, contexto)
            + "\n\nEscreva agora o rascunho da resposta."
        )

    def _montar_prompt_refinamento(
        self,
        *,
        anterior: str,
        instrucao: str,
        tarefa: Tarefa,
        contexto: dict | None,
    ) -> str:
        return (
            self._bloco_tarefa(tarefa, contexto)
            + "\n\nRASCUNHO ATUAL:\n"
            + '"""\n'
            + anterior
            + '\n"""\n\nINSTRUÇÃO DE REFINAMENTO:\n'
            + instrucao
            + "\n\nReescreva o rascunho aplicando a instrução. "
            + "Devolva APENAS o novo texto, sem comentários sobre o que mudou."
        )
