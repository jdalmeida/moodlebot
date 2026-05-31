"""Captura o HTML/screenshot/candidates de uma tela do Moodle UNISC.

Uso interativo — abre o navegador com a sessão persistida, navega pra URL
informada e espera você ir manualmente até a tela que quer inspecionar
(ex.: clicar em "Adicionar tarefa" pra chegar no formulário de envio).
A cada Enter, captura:

- HTML completo da página atual          → page.html
- Screenshot full page                    → page.png
- Lista de candidates (button/input/a)    → candidates.txt
- Metadata (url, title, mod detectado)    → meta.json

NUNCA clica em nada por conta própria. O ponto é colher dados pra ajustar
seletores do `submitter.py` sem chutar HTML.

Exemplo:
    python scripts/inspect_task.py https://portalvirtual.unisc.br/moodle/mod/quiz/view.php?id=12345
    python scripts/inspect_task.py https://portalvirtual.unisc.br/moodle/mod/assign/view.php?id=67890 --slug assign-texto

A cada captura, cria uma pasta data/inspect/<timestamp>_<slug>/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import _bootstrap  # noqa: F401

from playwright.async_api import Page, async_playwright

from src.config import settings
from src.logging_setup import setup_logging


# JS executado dentro da página para extrair candidates de forma uniforme.
# Pega todos os elementos clicáveis "interessantes" + algumas dicas de
# selector estável (id, name, data-*, role+text).
_EXTRACT_CANDIDATES_JS = r"""
() => {
  const out = [];
  const seen = new Set();

  function shortText(el) {
    const t = (el.innerText || el.textContent || '').trim();
    return t.length > 120 ? t.slice(0, 117) + '...' : t;
  }

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  }

  function entry(el, kind) {
    if (seen.has(el)) return;
    seen.add(el);
    const attrs = {};
    for (const a of el.attributes) attrs[a.name] = a.value;
    out.push({
      kind,
      tag: el.tagName.toLowerCase(),
      text: shortText(el),
      value: el.value || '',
      type: el.type || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      classes: el.className || '',
      href: el.getAttribute('href') || '',
      role: el.getAttribute('role') || '',
      aria_label: el.getAttribute('aria-label') || '',
      data_action: el.getAttribute('data-action') || '',
      data_region: el.getAttribute('data-region') || '',
      visible: visible(el),
      form_action: (el.form && el.form.action) || '',
    });
  }

  document.querySelectorAll('button').forEach(el => entry(el, 'button'));
  document.querySelectorAll('input[type=submit], input[type=button], input[type=reset]')
    .forEach(el => entry(el, 'input-' + (el.type || 'submit')));
  // Links que se parecem com botão (Moodle usa <a class="btn"> bastante).
  document.querySelectorAll('a').forEach(el => {
    const txt = (el.innerText || '').trim();
    if (!txt) return;
    const cls = el.className || '';
    if (cls.includes('btn') || cls.includes('action') || /Enviar|Salvar|Tentar|Editar|Adicionar|Continuar|Finalizar|Pr.xima|Submit|Save|Attempt/i.test(txt)) {
      entry(el, 'a');
    }
  });
  // Editores: contenteditable e iframes (Atto, TinyMCE, CKEditor)
  document.querySelectorAll('[contenteditable="true"]').forEach(el => entry(el, 'editable'));
  document.querySelectorAll('iframe').forEach(el => {
    out.push({
      kind: 'iframe',
      tag: 'iframe',
      text: '',
      value: '',
      type: '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      classes: el.className || '',
      href: el.getAttribute('src') || '',
      role: '',
      aria_label: el.getAttribute('title') || '',
      data_action: '',
      data_region: '',
      visible: true,
      form_action: '',
    });
  });
  // Inputs file (uploads)
  document.querySelectorAll('input[type=file]').forEach(el => entry(el, 'input-file'));

  return {
    url: location.href,
    title: document.title,
    candidates: out,
  };
}
"""


def _detect_mod(url: str) -> str | None:
    m = re.search(r"/mod/([a-z0-9]+)/", url)
    return m.group(1) if m else None


def _detect_cmid(url: str) -> str | None:
    qs = parse_qs(urlparse(url).query)
    return qs.get("id", [None])[0]


def _format_candidates(data: dict) -> str:
    """Render candidates como texto fácil de ler num PR/diff."""
    rows = data["candidates"]
    out: list[str] = []
    out.append(f"# url:   {data['url']}")
    out.append(f"# title: {data['title']}")
    out.append(f"# total candidates: {len(rows)}")
    out.append("")

    def fmt(c: dict, idx: int) -> str:
        head = f"[{idx:03d}] kind={c['kind']} tag={c['tag']} visible={c['visible']}"
        attrs = []
        for k in (
            "text",
            "value",
            "type",
            "id",
            "name",
            "role",
            "aria_label",
            "data_action",
            "data_region",
            "href",
            "form_action",
            "classes",
        ):
            v = c.get(k) or ""
            if v:
                # Multi-linha vira uma linha só.
                v = re.sub(r"\s+", " ", str(v)).strip()
                attrs.append(f"  {k:11s}= {v}")
        return head + "\n" + "\n".join(attrs)

    for i, c in enumerate(rows):
        out.append(fmt(c, i))
        out.append("")
    return "\n".join(out)


async def _ainput(prompt: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, lambda: input(prompt))


async def _capture(page: Page, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Coleta tudo via evaluate — não toca em nada.
    data: dict = await page.evaluate(_EXTRACT_CANDIDATES_JS)

    html = await page.content()
    (out_dir / "page.html").write_text(html, encoding="utf-8")

    await page.screenshot(path=str(out_dir / "page.png"), full_page=True)

    (out_dir / "candidates.txt").write_text(_format_candidates(data), encoding="utf-8")

    meta = {
        "url": data["url"],
        "title": data["title"],
        "mod": _detect_mod(data["url"]),
        "cmid": _detect_cmid(data["url"]),
        "candidates_count": len(data["candidates"]),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"  ✓ HTML       → {out_dir / 'page.html'}")
    print(f"  ✓ Screenshot → {out_dir / 'page.png'}")
    print(f"  ✓ Candidates → {out_dir / 'candidates.txt'} ({len(data['candidates'])} itens)")
    print(f"  ✓ Meta       → {out_dir / 'meta.json'}  (mod={meta['mod']}, cmid={meta['cmid']})")


async def _main(url: str, slug: str | None) -> int:
    setup_logging()

    storage_path = settings.moodle_storage_state
    if not storage_path.exists():
        print(
            f"ERRO: storage_state não encontrado em {storage_path}.\n"
            "Rode `python scripts/first_login.py` antes.",
            file=sys.stderr,
        )
        return 2

    base_out = Path("data/inspect")
    base_out.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("Inspetor de tela do Moodle — Moodlebot")
    print("=" * 70)
    print(f"URL inicial:  {url}")
    print(f"Storage:      {storage_path}")
    print(f"Output:       {base_out.resolve()}/<timestamp>_<slug>/")
    print()
    print("O navegador vai abrir. Navegue MANUALMENTE até a tela que quer")
    print("inspecionar (ex.: clicar em 'Adicionar tarefa' / 'Tentar quiz').")
    print("Pressione Enter aqui pra capturar a tela ATUAL.")
    print("Digite 'q' + Enter pra sair.")
    print()

    async with async_playwright() as pw:
        launch_kwargs: dict[str, object] = {"headless": False}
        if settings.browser_executable_path:
            launch_kwargs["executable_path"] = settings.browser_executable_path
        elif settings.browser_channel:
            launch_kwargs["channel"] = settings.browser_channel
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(storage_state=str(storage_path))
            context.set_default_timeout(settings.moodle_request_timeout_ms)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")

            if "login" in page.url.lower():
                print(
                    "AVISO: caiu na página de login. Sessão expirou? "
                    "Rode `python scripts/first_login.py` de novo.",
                    file=sys.stderr,
                )
                # Não aborta — talvez o usuário queira logar manualmente
                # e seguir inspecionando.

            i = 0
            while True:
                ans = await _ainput(
                    f">>> [captura {i+1}] Enter pra capturar, 'q' pra sair: "
                )
                if ans.strip().lower() == "q":
                    print("Saindo.")
                    return 0
                i += 1
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                cmid = _detect_cmid(page.url) or "nocmid"
                mod = _detect_mod(page.url) or "nomod"
                tag = slug or f"{mod}-{cmid}"
                out_dir = base_out / f"{ts}_{tag}"
                try:
                    await _capture(page, out_dir)
                except Exception as e:  # noqa: BLE001
                    print(f"ERRO ao capturar: {e}", file=sys.stderr)
        finally:
            await browser.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("url", help="URL inicial da atividade no Moodle UNISC")
    p.add_argument(
        "--slug",
        help="Nome curto pra etiquetar as pastas de captura "
        "(default: <mod>-<cmid> detectado da URL atual).",
        default=None,
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(asyncio.run(_main(args.url, args.slug)))
