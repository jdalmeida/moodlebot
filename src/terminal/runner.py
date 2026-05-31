"""Modo terminal — assistente interativo via stdin/stdout.

Equivalente ao fluxo do Telegram (`src/telegram_bot/handlers.py`), mas sem bot
nem token: o usuário interage direto no terminal. Reusa exatamente as mesmas
peças de domínio — `MoodleSession`, `coletar_tarefas_pendentes`, `upsert_tarefa`,
`Drafter`, `MoodleSubmitter`, `RascunhoRepo` — trocando só a camada de I/O.

Diferenças deliberadas em relação ao modo bot:
- Sem scheduler/notificações: o humano está presente, a coleta é sob demanda.
- Sem máquina de estado persistida (`ConversaRepo`): o estado vive em variáveis
  locais do REPL. Os rascunhos, porém, são gravados em `rascunhos` com um
  `chat_id` sentinela (`TERMINAL_CHAT_ID`) para manter paridade com o Telegram.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from ..agent.drafter import Drafter
from ..config import settings
from ..db import (
    RascunhoRepo,
    close_db,
    init_schema,
    list_tarefas_pendentes,
    upsert_tarefa,
)
from ..models import Tarefa
from ..moodle.scraper import coletar_tarefas_pendentes
from ..moodle.session import MoodleSession, SessionExpiredError
from ..moodle.submitter import MoodleSubmitter, SubmissaoAcidentalError


# chat_id sentinela para os rascunhos do terminal. Chats reais do Telegram são
# positivos e não-zero, então 0 nunca colide com eles.
TERMINAL_CHAT_ID = 0


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #


async def _perguntar(prompt: str) -> str:
    """Lê uma linha do stdin sem bloquear o event loop."""
    return (await asyncio.to_thread(input, prompt)).strip()


def _contexto_da_tarefa(tarefa: Tarefa) -> dict:
    """Serializa a Tarefa em dict para alimentar o Drafter.

    Espelha `_contexto_para_drafter` dos handlers do Telegram. Replicado aqui
    (em vez de importado) para que o modo terminal não dependa do pacote
    telegram nem dos seus imports.
    """
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


def _imprimir_tarefa(indice: int, tarefa: Tarefa) -> None:
    prazo = tarefa.prazo.astimezone().strftime("%Y-%m-%d %H:%M") if tarefa.prazo else "—"
    print(f"  [{indice}] {tarefa.titulo}")
    print(f"      curso: {tarefa.curso} | tipo: {tarefa.tipo.value} | prazo: {prazo}")
    print(f"      {tarefa.url}")


def _imprimir_rascunho(versao: int, conteudo: str) -> None:
    print()
    print(f"📝 Rascunho v{versao}")
    print("─" * 60)
    print(conteudo)
    print("─" * 60)


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


async def _revisar_tarefa(drafter: Drafter, tarefa: Tarefa) -> None:
    """Gera v1 e entra no sub-loop de refino/salvar/descartar para uma tarefa."""
    assert tarefa.id is not None
    contexto = _contexto_da_tarefa(tarefa)

    print("\n⏳ Gerando rascunho…")
    try:
        resultado = await drafter.gerar_rascunho(tarefa, contexto)
    except Exception as e:  # noqa: BLE001
        logger.exception("Falha ao gerar rascunho")
        print(f"❌ Falhou ao gerar rascunho: {e}")
        return

    rascunho = await RascunhoRepo.criar(
        tarefa_id=tarefa.id,
        chat_id=TERMINAL_CHAT_ID,
        conteudo=resultado.conteudo,
        prompt_usado=resultado.prompt,
    )
    _imprimir_rascunho(rascunho.versao, rascunho.conteudo)

    while True:
        print(
            "\nComandos: [salvar] salva no Moodle | [refazer] gera nova versão | "
            "[descartar] volta à lista"
        )
        print("Ou digite uma instrução de refino (texto livre).")
        entrada = await _perguntar("> ")
        cmd = entrada.lower()

        if not entrada:
            continue

        if cmd == "descartar":
            print("🗑 Descartado.")
            return

        if cmd == "salvar":
            print("⏳ Salvando no Moodle…")
            submitter = MoodleSubmitter()
            try:
                await submitter.salvar_rascunho(str(tarefa.url), rascunho.conteudo)
            except SubmissaoAcidentalError as e:
                logger.error("SUBMISSÃO ACIDENTAL detectada: {}", e)
                print(
                    "🚨 ALERTA: o submitter detectou que a tarefa pode ter sido "
                    "ENVIADA PARA AVALIAÇÃO em vez de salva como rascunho.\n"
                    f"   Detalhe: {e}\n"
                    "   Verifique no Moodle antes de prosseguir."
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("Falha ao salvar rascunho no Moodle")
                print(f"❌ Falhou ao salvar no Moodle: {e}")
                continue
            assert rascunho.id is not None
            await RascunhoRepo.marcar_salvo(rascunho.id)
            print(
                f"✅ Rascunho v{rascunho.versao} salvo no Moodle "
                "(não enviado para avaliação)."
            )
            return

        # 'refazer' = nova v1 do zero; texto livre = refino do rascunho atual.
        print("⏳ Gerando…")
        try:
            if cmd == "refazer":
                resultado = await drafter.gerar_rascunho(tarefa, contexto)
            else:
                resultado = await drafter.refinar_rascunho(
                    anterior=rascunho.conteudo,
                    instrucao=entrada,
                    tarefa=tarefa,
                    contexto=contexto,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("Falha ao gerar/refinar rascunho")
            print(f"❌ Falhou: {e}")
            continue

        rascunho = await RascunhoRepo.criar(
            tarefa_id=tarefa.id,
            chat_id=TERMINAL_CHAT_ID,
            conteudo=resultado.conteudo,
            prompt_usado=resultado.prompt,
        )
        _imprimir_rascunho(rascunho.versao, rascunho.conteudo)


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

    drafter = Drafter(api_key=settings.google_api_key, model=settings.llm_model)

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

            await _revisar_tarefa(drafter, tarefas[idx - 1])
    except (KeyboardInterrupt, EOFError):
        print("\nEncerrando…")
    finally:
        await session.stop()
        await close_db()
        logger.info("Moodlebot (terminal) encerrado")
