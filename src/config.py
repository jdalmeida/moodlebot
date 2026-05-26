"""Configuração central da aplicação.

Lê variáveis de ambiente (com fallback no `.env`) via `pydantic-settings`.
Exponhe um singleton `settings` consumido por todos os módulos.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuração tipada da aplicação."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Moodle ---
    moodle_base_url: str = Field(
        default="https://portalvirtual.unisc.br/moodle",
        description="URL base da instalação Moodle (sem barra no final).",
    )
    moodle_storage_state: Path = Field(
        default=Path("data/storage_state.json"),
        description="Arquivo onde o Playwright persiste cookies/localStorage.",
    )
    moodle_headless: bool = Field(
        default=True,
        description="Se True, o navegador roda sem janela. first_login.py ignora isso.",
    )
    moodle_request_timeout_ms: int = Field(default=30_000, ge=1_000)
    # Em distros novas demais para o Chromium empacotado do Playwright
    # (Ubuntu 26.04, p.ex.), usamos o Google Chrome do sistema via channel.
    # "" desativa o channel (Playwright tenta o Chromium baixado).
    browser_channel: str = Field(
        default="chrome",
        description="Canal do Playwright: 'chrome', 'msedge', 'chromium' ou '' para o Chromium baixado.",
    )
    # Override explícito (raro — só se o channel não der). Ex: /usr/bin/google-chrome
    browser_executable_path: str = Field(default="")

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", description="Token do BotFather.")
    # NoDecode evita que pydantic-settings tente fazer json.loads("111,222") antes
    # de chamar o validator abaixo — sem isso, o parser do .env quebra com lists.
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description="IDs de usuários autorizados a falar com o bot.",
    )

    # --- Banco ---
    database_path: Path = Field(default=Path("data/moodlebot.db"))

    # --- Scheduler ---
    poll_interval_minutes: int = Field(default=15, ge=1, le=60 * 24)
    log_level: str = Field(default="INFO")

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, raw: object) -> list[int]:
        # Aceita lista pronta (uso programático), string "1,2,3" (vindo do .env)
        # ou string vazia / None (lista vazia).
        if raw is None or raw == "":
            return []
        if isinstance(raw, list):
            return [int(x) for x in raw if str(x).strip()]
        if isinstance(raw, str):
            return [int(p.strip()) for p in raw.split(",") if p.strip()]
        raise TypeError(f"TELEGRAM_ALLOWED_USER_IDS inválido: {raw!r}")

    @field_validator("moodle_storage_state", "database_path", mode="after")
    @classmethod
    def _resolve_relative(cls, p: Path) -> Path:
        # Mantém caminhos relativos ancorados na raiz do projeto, evitando que
        # o cwd do processo afete onde o banco é criado.
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def moodle_login_url(self) -> str:
        return f"{self.moodle_base_url.rstrip('/')}/login/index.php"

    @property
    def moodle_dashboard_url(self) -> str:
        return f"{self.moodle_base_url.rstrip('/')}/my/"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
