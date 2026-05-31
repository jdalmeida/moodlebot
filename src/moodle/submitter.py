"""MoodleSubmitter — salva rascunho no Moodle SEM enviar para avaliação.

Princípios de segurança (não-negociáveis):

1. **Jamais clica em "Enviar tarefa" / "Enviar para avaliação"**. O fluxo só
   toca em "Salvar mudanças".
2. **Asserções antes e depois do click**:
   - Antes: confirma que o botão alvo é mesmo o de salvar (texto + selector).
   - Depois: lê o status final e dispara `SubmissaoAcidentalError` se aparecer
     qualquer indício de submissão.
3. **Browser próprio**: o submitter NÃO compartilha contexto com o
   `MoodleSession` do scheduler. Cria, usa e fecha o seu próprio browser
   (mesmo `storage_state`). Isso isola falhas e permite `MOODLE_DEBUG=true`
   abrir uma janela visível sem afetar a coleta paralela.

Os seletores são chutes baseados no Moodle 3.10 padrão e DEVEM ser ajustados
após a primeira execução manual com `MOODLE_DEBUG=true`.
"""

from __future__ import annotations

from loguru import logger
from playwright.async_api import Page, async_playwright

from ..config import settings


# =========================================================================== #
#  SELETORES — ajustar após primeira execução manual com MOODLE_DEBUG=true.
# =========================================================================== #

# Botão que abre o formulário de envio. Em Moodle 3.10 o link aparece com
# texto "Adicionar tarefa" (sem rascunho ainda) ou "Editar envio" (já existe).
SELECTOR_BOTAO_EDITAR_RESPOSTA = (
    'a:has-text("Adicionar tarefa"), a:has-text("Editar envio")'
)

# Área de resposta — Atto editor por padrão (contenteditable). Se a instalação
# usar CKEditor (iframe), trocar por `iframe.cke_wysiwyg_frame` + `body`.
SELECTOR_AREA_RESPOSTA = 'div#id_onlinetext_editoreditable[contenteditable="true"]'

# Botão de salvar (NÃO enviar). Moodle 3.10 usa <input type=submit>.
SELECTOR_BOTAO_SALVAR_MUDANCAS = (
    'input[type=submit][value*="Salvar"], button:has-text("Salvar mudanças")'
)

# Botão proibido — listado SÓ para nunca clicar acidentalmente.
SELECTOR_BOTAO_ENVIAR_AVALIACAO = (
    'input[type=submit][value*="Enviar"], button:has-text("Enviar tarefa")'
)

# Onde lemos o status final após salvar. Tabela de status do submission em
# /mod/assign/view.php — célula da direita da linha "Status do envio".
SELECTOR_STATUS_ENVIO = (
    "table.submissionstatustable td.lastcol, "
    "table.generaltable tr.submissionstatussubmitted td.lastcol, "
    "table.generaltable tr.submissionstatusdraft td.lastcol"
)

# Frases que indicam submissão acidental — qualquer match dispara erro.
FRASES_PROIBIDAS_STATUS = (
    "Enviado para avaliação",
    "Enviada para avaliação",
    "Submitted for grading",  # fallback en, caso o curso esteja em inglês
)

# Frase que confirma sucesso (rascunho salvo, NÃO enviado). Usada só em log.
FRASES_OK_RASCUNHO = (
    "Rascunho (não enviado)",
    "Draft (not submitted)",
)


# =========================================================================== #


class SubmissaoAcidentalError(RuntimeError):
    """O fluxo terminou com a tarefa ENVIADA — não era pra acontecer.

    Disparada quando o status final pós-click contém qualquer string de
    `FRASES_PROIBIDAS_STATUS`. Indica bug no submitter ou mudança no HTML
    do Moodle — investigar antes de tentar de novo.
    """


class MoodleSubmitter:
    """Submitter de uso único — cria browser próprio por chamada.

    Uso típico (dentro de um handler):
        submitter = MoodleSubmitter()
        await submitter.salvar_rascunho(tarefa.url, rascunho.conteudo)

    Headless é controlado por `MOODLE_DEBUG` (toggle independente de
    `MOODLE_HEADLESS`). Browser fechado no final, mesmo em erro.
    """

    async def salvar_rascunho(self, tarefa_url: str, conteudo: str) -> None:
        """Abre a tarefa, preenche resposta, clica 'Salvar mudanças', valida."""
        # MOODLE_DEBUG=true → headless=False; senão segue MOODLE_HEADLESS.
        headless = False if settings.moodle_debug else settings.moodle_headless
        logger.info(
            "Submitter iniciando: url={} debug={} headless={}",
            tarefa_url,
            settings.moodle_debug,
            headless,
        )

        async with async_playwright() as pw:
            launch_kwargs: dict[str, object] = {"headless": headless}
            if settings.browser_executable_path:
                launch_kwargs["executable_path"] = settings.browser_executable_path
            elif settings.browser_channel:
                launch_kwargs["channel"] = settings.browser_channel
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    storage_state=str(settings.moodle_storage_state)
                )
                context.set_default_timeout(settings.moodle_request_timeout_ms)
                page = await context.new_page()
                await self._fluxo(page, tarefa_url, conteudo)
            finally:
                await browser.close()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _fluxo(self, page: Page, tarefa_url: str, conteudo: str) -> None:
        await page.goto(tarefa_url, wait_until="domcontentloaded")
        if "login" in page.url.lower():
            raise RuntimeError(
                "Submitter caiu na página de login — sessão expirou? "
                "Rode `python scripts/first_login.py`."
            )

        # 1. Abre o formulário de edição.
        editar = page.locator(SELECTOR_BOTAO_EDITAR_RESPOSTA).first
        await editar.wait_for(state="visible")
        await editar.click()
        await page.wait_for_load_state("domcontentloaded")

        # 2. Preenche a área de resposta.
        area = page.locator(SELECTOR_AREA_RESPOSTA).first
        await area.wait_for(state="visible")
        # `fill` não funciona em contenteditable; usamos clear + type.
        await area.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await area.type(conteudo)

        # 3. Verificação DEFENSIVA antes do click final:
        salvar = page.locator(SELECTOR_BOTAO_SALVAR_MUDANCAS).first
        await salvar.wait_for(state="visible")
        # Atributo `value` para <input type=submit>, ou texto interno.
        rotulo = (await salvar.get_attribute("value")) or (
            await salvar.text_content()
        ) or ""
        rotulo = rotulo.strip()
        logger.info("Botão de salvar identificado com rótulo: {!r}", rotulo)
        if "Salvar" not in rotulo and "Save" not in rotulo:
            raise RuntimeError(
                f"Botão de salvar não confere — rótulo={rotulo!r}. "
                "Ajuste SELECTOR_BOTAO_SALVAR_MUDANCAS em submitter.py."
            )
        if "Enviar" in rotulo or "Submit" in rotulo:
            # Cinto + suspensório: se o seletor pegou o botão errado, aborta.
            raise RuntimeError(
                f"Botão suspeito de SUBMISSÃO casou o seletor de salvar: "
                f"rótulo={rotulo!r}. Aborta sem clicar."
            )

        # 4. Salva.
        await salvar.click()
        await page.wait_for_load_state("domcontentloaded")

        # 5. Verificação DEFENSIVA pós-click — leu o status final.
        await self._validar_status_final(page)

    async def _validar_status_final(self, page: Page) -> None:
        """Lê o status pós-save e dispara `SubmissaoAcidentalError` se algo
        indicar que a tarefa foi ENVIADA em vez de só salva como rascunho."""
        # O Moodle pode mostrar o status em várias linhas; concatena tudo.
        celulas = await page.locator(SELECTOR_STATUS_ENVIO).all_text_contents()
        status_concat = " | ".join(c.strip() for c in celulas if c.strip())
        logger.info("Status pós-save: {!r}", status_concat)

        for proibida in FRASES_PROIBIDAS_STATUS:
            if proibida.lower() in status_concat.lower():
                raise SubmissaoAcidentalError(
                    f"Status final indica envio: {proibida!r} encontrado em "
                    f"{status_concat!r}. NENHUMA marcação de salvo será feita."
                )

        # Sucesso "esperado" só loga; ausência não é erro (HTML pode variar).
        for ok in FRASES_OK_RASCUNHO:
            if ok.lower() in status_concat.lower():
                logger.info("Confirmado: rascunho salvo no Moodle ({!r})", ok)
                return

        logger.warning(
            "Não consegui confirmar o status como rascunho explicitamente, "
            "mas tampouco encontrei FRASES_PROIBIDAS — considerando OK. "
            "Status lido: {!r}",
            status_concat,
        )
