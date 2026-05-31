"""Sessão Playwright persistida contra o Moodle.

Princípios:
- Login institucional (SSO/SAML) é feito manualmente uma única vez via
  `scripts/first_login.py`, que grava `storage_state.json`.
- O agente carrega esse `storage_state` e nunca redigita credenciais.
- Uma única instância de browser/context é mantida viva durante a execução
  do agente — reaproveitar o navegador entre ticks evita ~2s de cold start
  por poll e preserva caches HTTP do Moodle.
- Quando o cookie expira, `is_logged_in()` retorna False e o agente lança
  `SessionExpiredError` — o caller decide como notificar o humano. **Nunca**
  tentamos relogin automático: não queremos credenciais no `.env`.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from loguru import logger
from playwright.async_api import (
    APIRequestContext,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from ..config import settings


class SessionExpiredError(RuntimeError):
    """Cookies expirados — precisamos rodar `first_login.py` de novo."""


class MoodleSession:
    """Wrapper async sobre Playwright para um único usuário do Moodle.

    Pode ser usado como context manager:

        async with MoodleSession() as page:
            await page.goto(...)

    ou via start/stop para vidas longas (caso do scheduler):

        session = MoodleSession()
        await session.start()
        ...
        page = await session.new_page()
        await session.stop()
    """

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._context is not None:
            return  # idempotente

        storage_path = settings.moodle_storage_state
        if not storage_path.exists():
            raise SessionExpiredError(
                f"storage_state não encontrado em {storage_path}. "
                "Rode `python scripts/first_login.py` primeiro."
            )

        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, object] = {"headless": settings.moodle_headless}
        if settings.browser_executable_path:
            launch_kwargs["executable_path"] = settings.browser_executable_path
        elif settings.browser_channel:
            launch_kwargs["channel"] = settings.browser_channel
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(storage_state=str(storage_path))
        self._context.set_default_timeout(settings.moodle_request_timeout_ms)
        logger.debug(
            "Sessão Moodle iniciada (headless={})", settings.moodle_headless
        )

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
        logger.debug("Sessão Moodle encerrada")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    # ------------------------------------------------------------------ #
    # Operações
    # ------------------------------------------------------------------ #

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("MoodleSession.start() não foi chamado")
        return await self._context.new_page()

    def request_context(self) -> APIRequestContext:
        """`APIRequestContext` da MESMA BrowserContext.

        Compartilha os cookies de sessão, então `GET` em `pluginfile.php` (e
        afins) baixa arquivos autenticados sem novo login. Usado pelo
        `reader.baixar_arquivo`.
        """
        if self._context is None:
            raise RuntimeError("MoodleSession.start() não foi chamado")
        return self._context.request

    async def is_logged_in(self) -> bool:
        """Verifica se a sessão ainda está válida abrindo o dashboard.

        Heurística: o Moodle redireciona requisições anônimas para /login/.
        Se a URL final contém 'login', o cookie expirou.
        """
        if self._context is None:
            raise RuntimeError("MoodleSession.start() não foi chamado")
        page = await self._context.new_page()
        try:
            await page.goto(settings.moodle_dashboard_url, wait_until="domcontentloaded")
            final_url = page.url.lower()
            logged_in = "login" not in final_url
            if not logged_in:
                logger.warning("Sessão expirou — URL final: {}", final_url)
            return logged_in
        finally:
            await page.close()

    async def ensure_logged_in(self) -> None:
        """Falha cedo se a sessão estiver expirada."""
        if not await self.is_logged_in():
            raise SessionExpiredError(
                "Cookie do Moodle expirou. Rode `python scripts/first_login.py`."
            )
