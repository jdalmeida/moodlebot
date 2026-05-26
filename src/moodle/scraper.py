"""Scraper do dashboard Moodle.

Estratégia: scraping HTML em vez de Web Services REST. O usuário comum do
Moodle geralmente não tem permissão para emitir token, então scraping é o
único caminho garantido. Se mais tarde o token vier, dá para trocar o
backend sem mexer no resto do pipeline (interface `coletar_tarefas_pendentes`
permanece igual).

Fonte primária: o bloco "Linha do tempo" (`/my/`) já vem filtrado por ações
que requerem atenção — perfeito para fase 1. Cobertura por curso individual
fica para fase 2.

Resiliência: o Moodle 3.10 renderiza esse bloco via AJAX. Esperamos pelo
seletor da timeline com timeout amplo e, se ele não aparecer, retornamos
lista vazia (em vez de explodir o tick inteiro).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from loguru import logger
from playwright.async_api import TimeoutError as PWTimeoutError

from ..config import settings
from ..models import Tarefa, TipoTarefa
from .session import MoodleSession


# ----------------------------------------------------------------------- #
# Mapeamento dos ícones / nomes de módulo do Moodle para o nosso enum
# ----------------------------------------------------------------------- #

_MOD_NAME_TO_TIPO: dict[str, TipoTarefa] = {
    "assign": TipoTarefa.ASSIGNMENT,
    "assignment": TipoTarefa.ASSIGNMENT,
    "quiz": TipoTarefa.QUIZ,
    "forum": TipoTarefa.FORUM,
    "choice": TipoTarefa.CHOICE,
    "workshop": TipoTarefa.WORKSHOP,
}


def _classify_tipo(url_or_class: str) -> TipoTarefa:
    """Tenta extrair o nome do módulo de uma URL `/mod/<nome>/...` ou de
    classes CSS no formato `mod_<nome>`."""
    m = re.search(r"/mod/([a-z0-9]+)/", url_or_class)
    if m and m.group(1) in _MOD_NAME_TO_TIPO:
        return _MOD_NAME_TO_TIPO[m.group(1)]
    m = re.search(r"mod_([a-z0-9]+)", url_or_class)
    if m and m.group(1) in _MOD_NAME_TO_TIPO:
        return _MOD_NAME_TO_TIPO[m.group(1)]
    return TipoTarefa.OTHER


def _extract_cmid(url: str) -> str | None:
    """O `cmid` (course module id) é o identificador estável de uma
    instância de atividade. Vem no query string como `?id=N`."""
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:  # noqa: BLE001
        return None
    if "id" in qs and qs["id"]:
        return qs["id"][0]
    return None


# ----------------------------------------------------------------------- #
# Parsing de prazos em pt-BR
# ----------------------------------------------------------------------- #

_MESES_PT = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "março": 3, "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}

# Fuso usado para interpretar datas exibidas pelo Moodle (BRT). Convertemos
# para UTC ao retornar — todo o resto da app trabalha em UTC.
_BRT = timezone(timedelta(hours=-3))


def _parse_prazo_ptbr(texto: str, agora: datetime | None = None) -> datetime | None:
    """Tenta interpretar uma string de prazo do Moodle em português.

    Cobre os formatos mais comuns:
      - "Vence em 3 dias"
      - "Atraso de 2 dias"  (já passou)
      - "amanhã, 23:59"
      - "hoje, 14:00"
      - "12 de junho, 23:59"
      - "12/06/2026, 23:59"
      - "2026-06-12T23:59:00"  (formato ISO direto, raro)

    Retorna sempre datetime aware UTC. Devolve None se não conseguir parsear —
    o caller deve tratar None como "sem prazo conhecido".
    """
    if not texto:
        return None
    texto = texto.strip().lower()
    agora = agora or datetime.now(_BRT)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=_BRT)

    # ISO direto
    try:
        dt = datetime.fromisoformat(texto.replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_BRT)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # "vence em N dias" / "em N dias"
    m = re.search(r"\bem\s+(\d+)\s+dias?\b", texto)
    if m:
        return (agora + timedelta(days=int(m.group(1)))).astimezone(timezone.utc)

    # "atraso de N dias" → prazo já passou
    m = re.search(r"atraso\s+de\s+(\d+)\s+dias?", texto)
    if m:
        return (agora - timedelta(days=int(m.group(1)))).astimezone(timezone.utc)

    # "hoje, HH:MM" / "amanhã, HH:MM" / "ontem, HH:MM"
    m = re.search(r"\b(hoje|amanhã|amanha|ontem)\b[,\s]+(\d{1,2}):(\d{2})", texto)
    if m:
        label, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        base = agora
        if label in ("amanhã", "amanha"):
            base = base + timedelta(days=1)
        elif label == "ontem":
            base = base - timedelta(days=1)
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0).astimezone(
            timezone.utc
        )

    # "DD de MES[, HH:MM]"  (Moodle pt-BR: "12 de junho, 23:59")
    m = re.search(
        r"(\d{1,2})\s+de\s+([a-zçãâéí]+)(?:\s+de\s+(\d{4}))?(?:[,\s]+(\d{1,2}):(\d{2}))?",
        texto,
    )
    if m:
        dia = int(m.group(1))
        mes_nome = m.group(2)
        ano = int(m.group(3)) if m.group(3) else agora.year
        hh = int(m.group(4)) if m.group(4) else 23
        mm = int(m.group(5)) if m.group(5) else 59
        if mes_nome in _MESES_PT:
            try:
                dt = datetime(
                    ano, _MESES_PT[mes_nome], dia, hh, mm, tzinfo=_BRT
                )
                return dt.astimezone(timezone.utc)
            except ValueError:
                pass

    # "DD/MM/YYYY[, HH:MM]" ou "DD/MM[, HH:MM]"
    m = re.search(
        r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?:[,\s]+(\d{1,2}):(\d{2}))?", texto
    )
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        ano = int(m.group(3)) if m.group(3) else agora.year
        if ano < 100:
            ano += 2000
        hh = int(m.group(4)) if m.group(4) else 23
        mm = int(m.group(5)) if m.group(5) else 59
        try:
            dt = datetime(ano, mes, dia, hh, mm, tzinfo=_BRT)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    return None


# ----------------------------------------------------------------------- #
# Extração do dashboard
# ----------------------------------------------------------------------- #


# Seletor amplo: o Moodle 3.10 usa `[data-region="event-list-content"]`
# para o bloco da Linha do Tempo. Em alguns temas custom o data-region
# muda, então também aceitamos `.timeline-event-list-container`.
_TIMELINE_SELECTORS = [
    '[data-region="event-list-content"]',
    ".timeline-event-list-container",
    "[data-region='timeline-view']",
]


async def coletar_tarefas_pendentes(session: MoodleSession) -> list[Tarefa]:
    """Visita o dashboard e devolve as tarefas pendentes listadas na timeline.

    Não escreve no banco — quem chama (o `tick` do scheduler) é responsável
    pelo UPSERT.
    """
    page = await session.new_page()
    try:
        await page.goto(
            settings.moodle_dashboard_url, wait_until="domcontentloaded"
        )

        if "login" in page.url.lower():
            # Sinaliza para o caller via session.is_logged_in() em vez de
            # levantar daqui — mantém esta função pura de scraping.
            logger.warning(
                "Dashboard redirecionou para login. Sessão provavelmente expirada."
            )
            return []

        # Aguarda a timeline aparecer. Se nenhum dos seletores aparece em ~8s,
        # assumimos que a conta não tem itens na linha do tempo.
        timeline_html: str | None = None
        for sel in _TIMELINE_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=8_000)
                timeline_html = await page.content()
                break
            except PWTimeoutError:
                continue

        if timeline_html is None:
            logger.info("Nenhum bloco de timeline encontrado no dashboard.")
            return []

        return list(
            _parse_dashboard_html(timeline_html, base_url=settings.moodle_base_url)
        )
    finally:
        await page.close()


def _parse_dashboard_html(html: str, *, base_url: str) -> Iterable[Tarefa]:
    """Parse pura — separada para ser testável sem Playwright."""
    soup = BeautifulSoup(html, "lxml")

    # Cada evento da timeline é um `[data-region="event-list-item"]`.
    items = soup.select('[data-region="event-list-item"]')
    if not items:
        # Fallback para temas mais antigos.
        items = soup.select(".timeline-event-list-item")

    for item in items:
        try:
            tarefa = _parse_event_item(item, base_url=base_url)
            if tarefa is not None:
                yield tarefa
        except Exception as e:  # noqa: BLE001
            logger.warning("Falha ao parsear item da timeline: {}", e)
            continue


def _parse_event_item(item: Tag, *, base_url: str) -> Tarefa | None:
    # Link principal do item (vai para a atividade)
    link = item.find("a", href=True)
    if not link:
        return None
    href = link["href"]
    url = href if href.startswith("http") else urljoin(base_url, href)

    cmid = _extract_cmid(url)
    if not cmid:
        # Sem cmid não temos chave estável — descartamos.
        return None

    titulo = link.get_text(strip=True) or "(sem título)"

    # Curso: vem em um elemento com data-region="event-list-course-name"
    # ou em link secundário com /course/view.php
    curso_tag = item.find(attrs={"data-region": "event-list-course-name"})
    if curso_tag is None:
        curso_link = item.find("a", href=re.compile(r"/course/view\.php"))
        curso = curso_link.get_text(strip=True) if curso_link else "(curso desconhecido)"
    else:
        curso = curso_tag.get_text(strip=True) or "(curso desconhecido)"

    # Tipo: tentamos pela URL da atividade, depois por classes do ícone.
    tipo = _classify_tipo(url)
    if tipo is TipoTarefa.OTHER:
        icon = item.find("img", class_=re.compile(r"activityicon|icon"))
        if icon is not None:
            src = icon.get("src") or ""
            tipo = _classify_tipo(src)

    # Prazo: o Moodle costuma renderizar a data como atributo `title` ou
    # texto em `[data-region="event-time"]`.
    prazo_texto = ""
    prazo_tag = item.find(attrs={"data-region": "event-time"})
    if prazo_tag is not None:
        prazo_texto = prazo_tag.get("title") or prazo_tag.get_text(strip=True)
    if not prazo_texto:
        # Procurar qualquer `time` ou span com classe `text-muted`
        t = item.find("time")
        if t is not None:
            prazo_texto = t.get("datetime") or t.get_text(strip=True)

    prazo = _parse_prazo_ptbr(prazo_texto)

    return Tarefa(
        moodle_id=cmid,
        tipo=tipo,
        curso=curso,
        titulo=titulo,
        url=url,
        prazo=prazo,
    )
