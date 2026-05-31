"""Ferramentas (function-calling) que o agente expõe ao Gemini.

`MoodleTools` é um adaptador fino entre o modelo e `src/moodle/reader.py`:
- `gemini_tool()` devolve o `types.Tool` com as declarações que o modelo enxerga.
- `dispatch(name, args)` executa a coroutine correspondente do reader.

Todas as ferramentas são SOMENTE-LEITURA. Nenhuma escreve no Moodle — escrita
continua exclusiva do `MoodleSubmitter`, fora deste conjunto.

Controles: budget global de chamadas (`agent_max_tool_calls`), cache por
(nome, args) para não rebaixar o mesmo arquivo, e acúmulo dos artefatos
coletados (enunciado + textos) para persistir/alimentar contexto depois.
"""

from __future__ import annotations

import json

from google.genai import types
from loguru import logger

from ..config import settings
from ..models import TipoTarefa
from ..moodle import reader
from ..moodle.session import MoodleSession, SessionExpiredError


class MoodleTools:
    """Conjunto de ferramentas vivas para uma execução do agente."""

    def __init__(self, session: MoodleSession) -> None:
        self._session = session
        self.calls_usadas = 0
        self._cache: dict[str, dict | list] = {}
        # Artefatos acumulados ao longo da execução, para persistir como contexto.
        self._enunciados: list[dict] = []
        self._materiais: list[dict] = []

    # ------------------------------------------------------------------ #
    # Schema exposto ao modelo
    # ------------------------------------------------------------------ #

    def gemini_tool(self) -> types.Tool:
        return types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="listar_cursos",
                    description=(
                        "Lista os cursos/disciplinas em que o aluno está "
                        "matriculado. Use para descobrir o contexto da "
                        "disciplina quando precisar navegar além da tarefa."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT, properties={}, required=[]
                    ),
                ),
                types.FunctionDeclaration(
                    name="obter_curso",
                    description=(
                        "Lista as seções e atividades de um curso (use o 'id' "
                        "vindo de listar_cursos). Útil para achar materiais de "
                        "aula relacionados à tarefa."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "curso_id": types.Schema(
                                type=types.Type.STRING,
                                description="ID numérico do curso.",
                            )
                        },
                        required=["curso_id"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="obter_atividade",
                    description=(
                        "Abre a página de uma atividade e devolve o enunciado "
                        "completo, prazo, status de envio, arquivos anexos e "
                        "links externos. COMECE por aqui, passando a URL da "
                        "tarefa que você recebeu."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "url_ou_cmid": types.Schema(
                                type=types.Type.STRING,
                                description=(
                                    "URL completa da atividade no Moodle, ou o "
                                    "cmid numérico (neste caso informe também o tipo)."
                                ),
                            ),
                            "tipo": types.Schema(
                                type=types.Type.STRING,
                                description=(
                                    "Opcional. Tipo do módulo: assignment, quiz, "
                                    "forum, choice ou workshop."
                                ),
                            ),
                        },
                        required=["url_ou_cmid"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="baixar_arquivo",
                    description=(
                        "Baixa um material anexo e devolve seu texto (PDF, DOCX, "
                        "PPTX, HTML ou TXT). Use APENAS com URLs vindas do campo "
                        "'anexos' de obter_atividade/obter_curso."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "url": types.Schema(
                                type=types.Type.STRING,
                                description="URL do arquivo (geralmente pluginfile.php).",
                            )
                        },
                        required=["url"],
                    ),
                ),
            ]
        )

    # ------------------------------------------------------------------ #
    # Despacho
    # ------------------------------------------------------------------ #

    async def dispatch(self, name: str, args: dict) -> dict | list:
        """Executa a ferramenta `name`. Erros viram `{"error": ...}` para o
        modelo se recuperar; `SessionExpiredError` é re-levantado (fatal)."""
        if self.calls_usadas >= settings.agent_max_tool_calls:
            return {"error": "limite global de chamadas de ferramenta atingido"}

        chave = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if chave in self._cache:
            return self._cache[chave]

        self.calls_usadas += 1
        try:
            resultado = await self._executar(name, args)
        except SessionExpiredError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Ferramenta {} falhou: {}", name, e)
            return {"error": str(e)}

        self._cache[chave] = resultado
        return resultado

    async def _executar(self, name: str, args: dict) -> dict | list:
        if name == "listar_cursos":
            cursos = await reader.listar_cursos(self._session)
            return [c.model_dump(mode="json") for c in cursos]

        if name == "obter_curso":
            secoes = await reader.obter_curso(self._session, str(args["curso_id"]))
            return [s.model_dump(mode="json") for s in secoes]

        if name == "obter_atividade":
            tipo = _parse_tipo(args.get("tipo"))
            det = await reader.obter_atividade(
                self._session, str(args["url_ou_cmid"]), tipo
            )
            dump = det.model_dump(mode="json")
            if det.enunciado_texto:
                self._enunciados.append(
                    {"atividade": det.nome, "texto": det.enunciado_texto}
                )
            return dump

        if name == "baixar_arquivo":
            conteudo = await reader.baixar_arquivo(self._session, str(args["url"]))
            dump = conteudo.model_dump(mode="json")
            self._materiais.append(
                {
                    "nome": conteudo.nome,
                    "url": conteudo.url,
                    "trecho": conteudo.texto[:4_000],
                    "truncado": conteudo.truncado,
                }
            )
            return dump

        return {"error": f"ferramenta desconhecida: {name}"}

    # ------------------------------------------------------------------ #
    # Artefatos coletados
    # ------------------------------------------------------------------ #

    def artefatos_para_contexto(self) -> dict:
        """Contexto enxuto (sem arquivos inteiros) para persistir/alimentar
        gerações posteriores."""
        return {
            "enunciados": self._enunciados,
            "materiais": self._materiais,
            "tool_calls": self.calls_usadas,
        }


def _parse_tipo(valor: object) -> TipoTarefa | None:
    if not valor:
        return None
    try:
        return TipoTarefa(str(valor).strip().lower())
    except ValueError:
        return None
