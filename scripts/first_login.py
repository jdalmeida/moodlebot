"""Realiza o login institucional no Moodle e salva `storage_state.json`.

Uso:
    python scripts/first_login.py

Abre uma janela do Chromium, navega para a página de login do Moodle e
**espera o usuário humano** concluir o SSO (provavelmente SAML do UNISC).
Quando o usuário pressiona Enter no terminal, captura o `storage_state` do
contexto e grava no caminho configurado em `.env`.

Este script é executado **uma única vez** (e novamente sempre que o cookie
expirar — geralmente quando o usuário desloga ou após semanas).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  — adiciona a raiz do repo ao sys.path

from playwright.async_api import async_playwright

from src.config import settings
from src.logging_setup import setup_logging


# Permitir Ctrl+C limpo enquanto esperamos input() em outra thread.
def _ainput(prompt: str) -> asyncio.Future[str]:
    return asyncio.get_event_loop().run_in_executor(None, lambda: input(prompt))


async def _main() -> int:
    setup_logging()
    storage_path: Path = settings.moodle_storage_state
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("Login institucional do Moodle — Moodlebot")
    print("=" * 70)
    print(f"Base URL: {settings.moodle_base_url}")
    print(f"Storage state será salvo em: {storage_path}")
    print()
    print("Será aberta uma janela do Chromium. Faça o login completo")
    print("(SSO institucional). Aguarde aparecer o dashboard do Moodle.")
    print("Quando estiver logado, volte aqui e pressione Enter.")
    print()

    async with async_playwright() as pw:
        # Sempre não-headless aqui: o usuário precisa interagir.
        launch_kwargs: dict[str, object] = {"headless": False}
        if settings.browser_executable_path:
            launch_kwargs["executable_path"] = settings.browser_executable_path
        elif settings.browser_channel:
            launch_kwargs["channel"] = settings.browser_channel
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(settings.moodle_login_url)

            await _ainput(">>> Pressione Enter quando estiver logado no dashboard... ")

            # Sanity check leve: tenta carregar o dashboard.
            try:
                await page.goto(
                    settings.moodle_dashboard_url, wait_until="domcontentloaded"
                )
                final_url = page.url.lower()
                if "login" in final_url:
                    print(
                        "AVISO: o navegador foi redirecionado para login "
                        f"({final_url}). O cookie pode não estar salvo.",
                        file=sys.stderr,
                    )
            except Exception as e:  # noqa: BLE001
                print(f"AVISO: não consegui verificar o dashboard: {e}", file=sys.stderr)

            await context.storage_state(path=str(storage_path))
            print(f"OK: storage_state salvo em {storage_path}")
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
