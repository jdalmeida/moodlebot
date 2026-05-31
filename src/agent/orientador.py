"""Orientador — agente que usa ferramentas para entender a tarefa e produzir
um ROTEIRO de como resolvê-la (não a resposta pronta).

Substitui o fluxo do `Drafter` (single-shot, resposta pronta) por um loop
agêntico de function-calling do google-genai: o modelo decide quando abrir a
atividade, baixar materiais e ler o contexto, usando as `MoodleTools`.

Loop MANUAL (automatic-function-calling do SDK desligado): as ferramentas são
async + Playwright e precisam de budget, cache, tratamento de sessão expirada e
truncamento da saída antes de voltar ao modelo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from google import genai
from google.genai import types
from loguru import logger

from ..config import settings
from ..models import Tarefa
from ..moodle.session import MoodleSession
from .tools import MoodleTools


SYSTEM_ORIENTADOR = """\
Você é um TUTOR acadêmico que orienta um estudante universitário a resolver uma \
tarefa do Moodle — sem fazer a tarefa por ele.

Você tem ferramentas para navegar no Moodle: leia o enunciado completo da \
atividade (obter_atividade), baixe e leia os materiais de aula anexos \
(baixar_arquivo) e, se precisar de contexto da disciplina, explore o curso \
(listar_cursos / obter_curso). SEMPRE comece chamando obter_atividade com a URL \
da tarefa.

Depois de entender o contexto, produza uma ORIENTAÇÃO em português brasileiro, \
em markdown, com estas seções:
1. **O que a tarefa pede** — interpretação clara do enunciado e dos critérios.
2. **Conceitos-chave** — o que o aluno precisa dominar para resolver.
3. **Materiais relevantes** — quais arquivos/aulas ajudam, citando de onde vêm.
4. **Passo a passo sugerido** — um roteiro de ação, não a resposta.
5. **Perguntas para o aluno responder** — para ele construir a própria solução.

REGRAS:
- NÃO escreva a resposta final, redação pronta, código completo ou solução do \
exercício. Oriente, não entregue.
- Baseie-se no que você leu via ferramentas; se faltar informação, diga o que \
está faltando em vez de inventar.
- Seja específico e prático.
"""


@dataclass(frozen=True, slots=True)
class Orientacao:
    """Resultado de uma execução do Orientador."""

    roteiro: str
    contexto: dict
    transcript: list[str] = field(default_factory=list)


@dataclass
class _AgentRun:
    texto: str
    transcript: list[str]


def _clamp_json(valor, max_chars: int):
    """Trunca recursivamente strings grandes para caber no prompt."""
    if isinstance(valor, str):
        return valor[:max_chars] + "…[truncado]" if len(valor) > max_chars else valor
    if isinstance(valor, list):
        return [_clamp_json(v, max_chars) for v in valor]
    if isinstance(valor, dict):
        return {k: _clamp_json(v, max_chars) for k, v in valor.items()}
    return valor


def _resumo_resultado(resultado) -> str:
    """Linha curta para o transcript de debug."""
    if isinstance(resultado, dict) and "error" in resultado:
        return f"erro: {resultado['error']}"
    if isinstance(resultado, list):
        return f"{len(resultado)} item(ns)"
    if isinstance(resultado, dict):
        chaves = ", ".join(list(resultado.keys())[:6])
        return f"{{{chaves}}}"
    return str(resultado)[:80]


class Orientador:
    """Gera orientações (roteiros) para tarefas, com acesso ao Moodle."""

    def __init__(self, api_key: str, model: str, session: MoodleSession) -> None:
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY não configurado — o Orientador não pode iniciar."
            )
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._session = session
        logger.info("Orientador pronto: model={}", model)

    async def orientar(self, tarefa: Tarefa) -> Orientacao:
        tools = MoodleTools(self._session)
        prompt = self._montar_prompt(tarefa)
        run = await self._run_agent(prompt, tools)
        return Orientacao(
            roteiro=run.texto,
            contexto=tools.artefatos_para_contexto(),
            transcript=run.transcript,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _montar_prompt(tarefa: Tarefa) -> str:
        partes = [
            "Oriente o aluno a resolver esta tarefa do Moodle.",
            "",
            f"TAREFA: {tarefa.titulo}",
            f"CURSO: {tarefa.curso}",
            f"TIPO: {tarefa.tipo.value}",
            f"URL: {tarefa.url}",
        ]
        if tarefa.prazo:
            partes.append(f"PRAZO: {tarefa.prazo.isoformat()}")
        if tarefa.descricao:
            partes.append(f"RESUMO CONHECIDO:\n{tarefa.descricao}")
        partes.append(
            "\nUse as ferramentas para ler o enunciado completo e os materiais "
            "antes de orientar."
        )
        return "\n".join(partes)

    def _config(self, *, com_tools: bool, tools: MoodleTools) -> types.GenerateContentConfig:
        kwargs: dict = {
            "system_instruction": SYSTEM_ORIENTADOR,
            "temperature": settings.agent_temperature,
        }
        if com_tools:
            kwargs["tools"] = [tools.gemini_tool()]
            kwargs["automatic_function_calling"] = (
                types.AutomaticFunctionCallingConfig(disable=True)
            )
        return types.GenerateContentConfig(**kwargs)

    async def _run_agent(self, prompt_inicial: str, tools: MoodleTools) -> _AgentRun:
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt_inicial)])
        ]
        transcript: list[str] = []
        cfg = self._config(com_tools=True, tools=tools)
        teto = settings.agent_tool_result_max_chars

        for _ in range(settings.agent_max_iterations):
            resp = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=cfg
            )
            fcs = resp.function_calls or []
            if not fcs:
                return _AgentRun(texto=(resp.text or "").strip(), transcript=transcript)

            # Preserva os parts do modelo (com os function_call) no histórico.
            contents.append(resp.candidates[0].content)

            tool_parts: list[types.Part] = []
            for fc in fcs:
                args = dict(fc.args or {})
                resultado = await tools.dispatch(fc.name, args)
                transcript.append(
                    f"{fc.name}({args}) -> {_resumo_resultado(resultado)}"
                )
                tool_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": _clamp_json(resultado, teto)},
                    )
                )
            contents.append(types.Content(role="user", parts=tool_parts))

        # Estourou o budget de iterações — força uma geração final sem tools.
        logger.warning(
            "Orientador atingiu agent_max_iterations; finalizando sem ferramentas."
        )
        return await self._finalizar_sem_tools(contents, tools, transcript)

    async def _finalizar_sem_tools(
        self,
        contents: list[types.Content],
        tools: MoodleTools,
        transcript: list[str],
    ) -> _AgentRun:
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(
                            "Pare de usar ferramentas. Com o que você já coletou, "
                            "produza agora a ORIENTAÇÃO final no formato pedido."
                        )
                    )
                ],
            )
        )
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=self._config(com_tools=False, tools=tools),
        )
        return _AgentRun(texto=(resp.text or "").strip(), transcript=transcript)
