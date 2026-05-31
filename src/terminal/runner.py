"""Modo terminal — assistente interativo via stdin/stdout.

Equivalente ao fluxo do Telegram (`src/telegram_bot/handlers.py`), mas sem bot
nem token: o usuário interage direto no terminal. Reusa as mesmas peças de
domínio — `MoodleSession`, `coletar_tarefas_pendentes`, `upsert_tarefa`,
`Orientador` — trocando só a camada de I/O.

Diferenças deliberadas em relação ao modo bot:
- Sem scheduler/notificações: o humano está presente, a coleta é sob demanda.
- Sem máquina de estado persistida (`ConversaRepo`): o estado vive em variáveis
  locais do REPL.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from ..agent.orientador import Orientador
from ..config import settings
from ..db import (
    close_db,
    init_schema,
    list_tarefas_pendentes,
    upsert_tarefa,
)
from ..models import Tarefa
from ..moodle.scraper import coletar_tarefas_pendentes
from ..moodle.session import MoodleSession, SessionExpiredError


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #


async def _perguntar(prompt: str) -> str:
    """Lê uma linha do stdin sem bloquear o event loop."""
    return (await asyncio.to_thread(input, prompt)).strip()


def _imprimir_tarefa(indice: int, tarefa: Tarefa) -> None:
    prazo = tarefa.prazo.astimezone().strftime("%Y-%m-%d %H:%M") if tarefa.prazo else "—"
    print(f"  [{indice}] {tarefa.titulo}")
    print(f"      curso: {tarefa.curso} | tipo: {tarefa.tipo.value} | prazo: {prazo}")
    print(f"      {tarefa.url}")


def _imprimir_orientacao(roteiro: str, transcript: list[str]) -> None:
    print()
    print("🧭 Orientação")
    print("─" * 60)
    print(roteiro)
    print("─" * 60)
    if transcript:
        print("(ferramentas usadas:)")
        for linha in transcript:
            print(f"  · {linha}")


# --------------------------------------------------------------------------- #
# Coleta
# --------------------------------------------------------------------------- #


async def _coletar(session: MoodleSession) -> list[Tarefa]:
    """Reusa o miolo do `tick`: garante login, coleta e faz upsert.

    Retorna a lista de tarefas pendentes do banco (após upsert).
    """
    await session.ensure_logged_in()
    tarefas = await coletar_tarefas_pendentes(session)
    logger.info("Coletadas {} tarefa(s) do dashboard", len(tarefas))
    for t in tarefas:
        try:
            await upsert_tarefa(t)
        except Exception as e:  # noqa: BLE001
            logger.error("Falha ao salvar tarefa {!r}: {}", t.moodle_id, e)
    return await list_tarefas_pendentes()


# --------------------------------------------------------------------------- #
# Loop de revisão de uma tarefa
# --------------------------------------------------------------------------- #


async def _revisar_tarefa(orientador: Orientador, tarefa: Tarefa) -> None:
    """Gera a orientação e entra no sub-loop de re-orientar/fechar."""
    assert tarefa.id is not None

    while True:
        print("\n⏳ Analisando a tarefa (lendo enunciado e materiais)…")
        try:
            orientacao = await orientador.orientar(tarefa)
        except SessionExpiredError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("Falha ao orientar")
            print(f"❌ Falhou ao gerar orientação: {e}")
            return

        _imprimir_orientacao(orientacao.roteiro, orientacao.transcript)

        print("\nComandos: [orientar] gera de novo | [fechar] volta à lista")
        entrada = await _perguntar("> ")
        cmd = entrada.lower()

        if cmd in ("fechar", "f", "", "q"):
            return
        if cmd in ("orientar", "r"):
            continue
        print("Comando não reconhecido — use [orientar] ou [fechar].")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def run_terminal() -> None:
    """Boot + REPL interativo. Espelha _post_init/_post_shutdown sem bot/scheduler."""
    await init_schema()

    session = MoodleSession()
    try:
        await session.start()
    except SessionExpiredError as e:
        print(f"⚠️  {e}")
        print("Rode `python scripts/first_login.py` e tente de novo.")
        await close_db()
        return

    orientador = Orientador(
        api_key=settings.google_api_key, model=settings.llm_model, session=session
    )

    print("\n🤖 Moodlebot — modo terminal. Digite 'q' a qualquer momento para sair.\n")

    try:
        tarefas: list[Tarefa] = []
        precisa_coletar = True

        while True:
            if precisa_coletar:
                print("⏳ Coletando tarefas do Moodle…")
                try:
                    tarefas = await _coletar(session)
                except SessionExpiredError as e:
                    print(f"⚠️  {e}")
                    print("Rode `python scripts/first_login.py` e tente de novo.")
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("Falha ao coletar tarefas")
                    print(f"❌ Falhou ao coletar tarefas: {e}")
                precisa_coletar = False

            print()
            if not tarefas:
                print("Nenhuma tarefa pendente. 🎉")
            else:
                print(f"Tarefas pendentes ({len(tarefas)}):")
                for i, t in enumerate(tarefas, start=1):
                    _imprimir_tarefa(i, t)

            print("\nEscolha o número de uma tarefa, [r] recoletar ou [q] sair.")
            escolha = await _perguntar("> ")
            cmd = escolha.lower()

            if cmd == "q":
                print("Até logo! 👋")
                return
            if cmd == "r":
                precisa_coletar = True
                continue

            try:
                idx = int(escolha)
            except ValueError:
                print("Entrada inválida.")
                continue
            if not (1 <= idx <= len(tarefas)):
                print("Número fora do intervalo.")
                continue

            await _revisar_tarefa(orientador, tarefas[idx - 1])
    except (KeyboardInterrupt, EOFError):
        print("\nEncerrando…")
    finally:
        await session.stop()
        await close_db()
        logger.info("Moodlebot (terminal) encerrado")
