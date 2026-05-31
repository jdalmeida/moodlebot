# Moodlebot

Agente autônomo que monitora tarefas pendentes em uma instalação Moodle 3.10.9
(UNISC), avisa pelo Telegram e — quando você pede — **orienta** como resolver
cada tarefa (lê o enunciado e os materiais e devolve um roteiro, não a resposta
pronta).

- Sessão Moodle persistida via `storage_state` do Playwright (login
  institucional feito uma única vez, manualmente).
- Scraping do dashboard para extrair tarefas pendentes.
- Dois modos de execução (`RUN_MODE`): **bot do Telegram** ou **REPL no
  terminal**, ambos sobre as mesmas peças de domínio.
- Notificações `nova`, `lembrete_24h`, `lembrete_2h` com idempotência via SQLite.
- Scheduler (APScheduler) que faz poll periódico (modo Telegram).
- **Orientador** agêntico via Gemini (`google-genai` + function-calling): o
  modelo decide quais ferramentas do Moodle usar para entender a tarefa antes
  de montar o roteiro. Todas as ferramentas são **somente-leitura**.

## Requisitos

- Python 3.11+
- Acesso à internet (para SSO no primeiro login, API do Telegram e API do Gemini)
- `GOOGLE_API_KEY` do [Google AI Studio](https://aistudio.google.com/apikey)
  (para o Orientador)
- Para o modo Telegram:
  - Token de bot do Telegram ([@BotFather](https://t.me/BotFather))
  - Seu `user_id` no Telegram ([@userinfobot](https://t.me/userinfobot))

## Setup

### Windows (automático)

Há um instalador que faz tudo (venv, dependências, navegador, `.env`, banco e
primeiro login):

```bat
setup.bat   REM cria o ambiente e configura — rode uma vez
run.bat     REM inicia o bot depois de configurado
```

### Linux / macOS (manual)

```bash
# 1. Criar ambiente virtual
python -m venv .venv
source .venv/bin/activate
#   Windows (cmd):        .venv\Scripts\activate.bat
#   Windows (PowerShell): .venv\Scripts\Activate.ps1

# 2. Instalar dependências
pip install -r requirements.txt

# 3. Garantir um Chrome/Chromium disponível para o Playwright.
#    Tente primeiro o Chromium oficial do Playwright:
python -m playwright install chromium
#    Se sua distro for nova demais e o comando acima falhar
#    (ex.: "Playwright does not support chromium on ubuntu26.04-x64"),
#    pule esse passo e use o Google Chrome do sistema. Garanta que ele
#    está instalado (`which google-chrome`) e mantenha o padrão
#    BROWSER_CHANNEL=chrome no `.env` (já é o default).

# 4. Configurar variáveis
cp .env.example .env
#   Edite .env: GOOGLE_API_KEY sempre; no modo telegram, também
#   TELEGRAM_BOT_TOKEN e TELEGRAM_ALLOWED_USER_IDS.

# 5. Criar o banco SQLite
python scripts/init_db.py

# 6. Login institucional (uma única vez — abre navegador)
python scripts/first_login.py
#   Faça o SSO completo, aguarde aparecer o dashboard, então pressione Enter.

# 7. Rodar
python main.py
```

No modo Telegram, mande `/ping` ao seu bot — você deve receber `pong`.

### Modo de execução: Telegram ou terminal

A variável `RUN_MODE` no `.env` escolhe como o agente roda:

- `RUN_MODE=telegram` (padrão) — roda como bot do Telegram (long polling).
  Precisa de `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_USER_IDS`. Inclui o
  scheduler e as notificações automáticas.
- `RUN_MODE=terminal` — assistente interativo no próprio terminal
  (stdin/stdout), sem precisar de bot/token nem scheduler. Coleta as tarefas
  sob demanda, lista, e você escolhe uma para o agente **orientar**. Dá para
  sobrescrever só para uma execução:

  ```bash
  RUN_MODE=terminal python main.py
  ```

## Configuração (`.env`)

| Variável                    | Padrão                                          | Descrição                                                           |
|-----------------------------|-------------------------------------------------|---------------------------------------------------------------------|
| `RUN_MODE`                  | `telegram`                                       | `telegram` (bot) ou `terminal` (REPL).                              |
| `MOODLE_BASE_URL`           | `https://portalvirtual.unisc.br/moodle`          | URL base da instalação Moodle.                                     |
| `MOODLE_STORAGE_STATE`      | `data/storage_state.json`                        | Onde o Playwright persiste cookies/localStorage.                   |
| `MOODLE_HEADLESS`           | `true`                                           | Navegador sem janela no scheduler (`first_login.py` ignora).       |
| `BROWSER_CHANNEL`           | `chrome`                                          | `chrome`, `msedge`, `chromium` ou `""` (Chromium baixado).         |
| `BROWSER_EXECUTABLE_PATH`   | _(vazio)_                                         | Override explícito do binário do navegador (raro).                 |
| `TELEGRAM_BOT_TOKEN`        | _(vazio)_                                         | Token do BotFather (modo telegram).                               |
| `TELEGRAM_ALLOWED_USER_IDS` | _(vazio)_                                         | IDs autorizados, separados por vírgula.                            |
| `DATABASE_PATH`             | `data/moodlebot.db`                              | Arquivo SQLite.                                                    |
| `POLL_INTERVAL_MINUTES`     | `15`                                             | Intervalo do poll do scheduler.                                   |
| `LOG_LEVEL`                 | `INFO`                                           | Nível do loguru.                                                   |
| `GOOGLE_API_KEY`            | _(vazio)_                                         | Chave do Google AI Studio (Gemini).                               |
| `LLM_MODEL`                 | `gemini-3.5-flash`                               | ID do modelo no `google-genai`.                                   |
| `MOODLE_DEBUG`              | `false`                                           | Abre navegador visível só no submitter (independe de `MOODLE_HEADLESS`). |

Limites do agente e do leitor de materiais (`agent_max_iterations`,
`agent_max_tool_calls`, `reader_max_download_bytes`, etc.) ficam em
`src/config.py`.

## Estrutura

```
moodlebot/
├── main.py                   # Entry point (ajusta sys.path e chama src.app.run)
├── setup.bat / run.bat       # Instalador e launcher para Windows
├── scripts/
│   ├── _bootstrap.py         # Ajuste de sys.path para rodar scripts soltos
│   ├── first_login.py        # Login interativo (uma vez) → storage_state
│   ├── init_db.py            # Cria o schema SQLite
│   └── inspect_task.py       # Captura HTML/screenshot/candidates de uma tela (ajuste de seletores)
├── src/
│   ├── app.py                # Orquestrador async (DB + Moodle + Telegram + Scheduler)
│   ├── config.py             # Settings via pydantic-settings
│   ├── models.py             # Modelos pydantic do domínio
│   ├── logging_setup.py      # loguru
│   ├── scheduler.py          # APScheduler (poll periódico)
│   ├── agent/
│   │   ├── orientador.py     # Agente ATIVO: loop de function-calling → roteiro
│   │   ├── tools.py          # Ferramentas Moodle (somente-leitura) expostas ao modelo
│   │   └── drafter.py        # Geração/refino de rascunho (componente de fase 2, ver abaixo)
│   ├── moodle/
│   │   ├── session.py        # Playwright + storage_state
│   │   ├── scraper.py        # Parser do dashboard
│   │   ├── reader.py         # Lê curso/atividade + baixa e extrai materiais
│   │   └── submitter.py      # Salva rascunho no Moodle SEM enviar (fase 2, ver abaixo)
│   ├── notifications/
│   │   ├── policy.py         # Regras de envio
│   │   └── sender.py         # Telegram + persistência de notificações
│   ├── telegram_bot/
│   │   ├── bot.py            # Factory da Application (registra handlers)
│   │   ├── handlers.py       # Comandos + callbacks do fluxo de orientação
│   │   ├── auth.py           # Allowlist (decorator require_allowed_user)
│   │   └── formatters.py     # Render para Telegram
│   ├── terminal/
│   │   └── runner.py         # REPL interativo (RUN_MODE=terminal)
│   └── db/
│       ├── conn.py           # aiosqlite + init_schema
│       ├── tarefa.py         # DAOs de tarefas e notificações
│       ├── repository.py     # Repos de Rascunho e Conversa
│       └── schema.sql        # DDL (single source of truth)
└── data/                     # SQLite e storage_state (gitignored)
```

## Comandos do bot

| Comando   | Descrição                                                   |
|-----------|-------------------------------------------------------------|
| `/start`  | Mensagem de boas-vindas e lista de comandos.                |
| `/ping`   | Verifica se o bot está vivo. Responde `pong` + uptime.      |
| `/tarefas`| Lista tarefas pendentes atualmente conhecidas no banco.     |

Apenas IDs presentes em `TELEGRAM_ALLOWED_USER_IDS` recebem resposta — os
demais são silenciosamente ignorados.

Em cada notificação de tarefa há o botão **🧭 Orientar**: o agente abre a
atividade no Moodle, lê o enunciado completo, baixa os materiais anexos
(PDF/DOCX/PPTX) e devolve um **roteiro** de como resolver — o que a tarefa pede,
conceitos-chave, materiais relevantes e passo a passo —, em vez de uma resposta
pronta. No roteiro há os botões **🔄 Re-orientar** (gera de novo) e
**✖ Fechar**.

## Política de notificações

| Tipo            | Quando dispara                                    |
|-----------------|---------------------------------------------------|
| `nova`          | Tarefa detectada pela primeira vez.               |
| `lembrete_24h`  | Prazo entre `now+22h` e `now+26h`, ainda pendente.|
| `lembrete_2h`   | Prazo entre `now+1h` e `now+3h`, ainda pendente.  |

A janela larga absorve o jitter do poll (15 min por padrão). Cada par
`(tarefa, tipo_notif)` é enviado **no máximo uma vez** (`UNIQUE` no banco).

> **Atenção**: se o bot estiver offline quando uma janela de 2h passar, a
> notificação **não** dispara retroativamente — é o trade-off por simplicidade
> nesta fase.

## Atualizando a sessão Moodle

Se o cookie expirar, o agente para de coletar tarefas e te avisa pelo Telegram.
Resolva rodando novamente:

```bash
python scripts/first_login.py
```

## Agente de orientação (LLM)

O agente usa o **Gemini** (`google-genai`) com *function-calling*: o modelo tem
ferramentas para navegar no Moodle e decide sozinho quais usar para entender a
tarefa antes de orientar. O loop de tool-calls é manual (automatic
function-calling do SDK desligado), pois as ferramentas são async + Playwright
e precisam de budget, cache e tratamento de sessão expirada.

- `src/moodle/reader.py` — `obter_atividade` (enunciado/prazo/status/anexos),
  `obter_curso`, `listar_cursos`, `baixar_arquivo` (download autenticado via a
  mesma sessão Playwright + extração de texto de PDF/DOCX/PPTX/HTML).
- `src/agent/tools.py` — expõe `listar_cursos`, `obter_curso`,
  `obter_atividade` e `baixar_arquivo` como ferramentas (somente-leitura), com
  budget de chamadas, cache e truncamento da saída antes de voltar ao modelo.
- `src/agent/orientador.py` — loop manual de tool-calls e a `Orientacao` final
  (roteiro em markdown + contexto + transcript das ferramentas usadas).

Coleta **híbrida**: a cada poll, tarefas novas têm o enunciado completo lido e
salvo automaticamente; o download pesado de materiais só roda quando você pede
**Orientar**.

> Todas as ferramentas do agente são **somente-leitura** — nunca escrevem no
> Moodle.

</content>
</invoke>
