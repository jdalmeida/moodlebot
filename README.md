# Moodlebot

Agente autônomo que monitora tarefas pendentes em uma instalação Moodle 3.10.9
(UNISC) e notifica via Telegram.

- Sessão Moodle persistida via `storage_state` do Playwright (login institucional feito uma única vez, manualmente).
- Scraping do dashboard para extrair tarefas pendentes.
- Bot Telegram com `/start`, `/ping`, `/tarefas`.
- Notificações `nova`, `lembrete_24h`, `lembrete_2h` com idempotência via SQLite.
- Scheduler (APScheduler) que faz poll periódico.

## Requisitos

- Python 3.11+
- Acesso à internet (para SSO no primeiro login e API do Telegram)
- Token de bot do Telegram ([@BotFather](https://t.me/BotFather))
- Seu `user_id` no Telegram ([@userinfobot](https://t.me/userinfobot))

## Setup

```bash
# 1. Criar ambiente virtual
python -m venv .venv

# Linux / macOS:
source .venv/bin/activate
# Windows (cmd):
#   .venv\Scripts\activate.bat
# Windows (PowerShell):
#   .venv\Scripts\Activate.ps1

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
#   Edite .env e preencha TELEGRAM_BOT_TOKEN e TELEGRAM_ALLOWED_USER_IDS

# 5. Criar o banco SQLite
python scripts/init_db.py

# 6. Login institucional (uma única vez — abre navegador)
python scripts/first_login.py
#   Faça o SSO completo, aguarde aparecer o dashboard, então pressione Enter.

# 7. Rodar o bot
python main.py
```

No Telegram, mande `/ping` ao seu bot — você deve receber `pong`.

### Modo de execução: Telegram ou terminal

A variável `RUN_MODE` no `.env` escolhe como o agente roda:

- `RUN_MODE=telegram` (padrão) — roda como bot do Telegram (polling). Precisa de
  `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_USER_IDS`.
- `RUN_MODE=terminal` — assistente interativo no próprio terminal (stdin/stdout),
  sem precisar de bot/token. Coleta as tarefas, lista, e você escolhe uma para
  o agente **orientar** (ler enunciado + materiais e montar um roteiro de como
  resolver). Também dá para sobrescrever só para uma execução:

  ```bash
  RUN_MODE=terminal python main.py
  ```

## Estrutura

```
moodlebot/
├── main.py                  # Entry point
├── scripts/
│   ├── first_login.py       # Login interativo (uma vez)
│   └── init_db.py           # Cria schema SQLite
├── src/
│   ├── app.py               # Orquestrador async
│   ├── config.py            # Settings via pydantic-settings
│   ├── db.py                # aiosqlite + DAOs
│   ├── models.py            # pydantic models
│   ├── logging_setup.py     # loguru
│   ├── agent/
│   │   ├── orientador.py    # Agente: loop de function-calling (Gemini)
│   │   └── tools.py         # Ferramentas Moodle expostas ao modelo
│   ├── moodle/
│   │   ├── session.py       # Playwright + storage_state
│   │   ├── scraper.py       # Parser do dashboard
│   │   └── reader.py        # Lê curso/atividade + baixa e extrai materiais
│   ├── notifications/
│   │   ├── policy.py        # Regras de envio
│   │   └── sender.py        # Telegram + persistência
│   ├── scheduler.py         # APScheduler
│   └── telegram_bot/
│       ├── auth.py          # Allowlist
│       ├── bot.py           # Handlers
│       └── formatters.py    # Render para Telegram
└── data/                    # SQLite e storage_state (gitignored)
```

## Comandos do bot

| Comando   | Descrição                                                   |
|-----------|-------------------------------------------------------------|
| `/start`  | Mensagem de boas-vindas e lista de comandos.                |
| `/ping`   | Verifica se o bot está vivo. Responde `pong`.               |
| `/tarefas`| Lista tarefas pendentes atualmente conhecidas no banco.     |

Apenas IDs presentes em `TELEGRAM_ALLOWED_USER_IDS` recebem resposta — os
demais são silenciosamente ignorados.

Em cada notificação de tarefa há o botão **🧭 Orientar**: o agente abre a
atividade no Moodle, lê o enunciado completo, baixa os materiais anexos
(PDF/DOCX/PPTX) e devolve um **roteiro** de como resolver — conceitos-chave,
materiais relevantes e passo a passo —, em vez de uma resposta pronta.

## Política de notificações

| Tipo            | Quando dispara                                    |
|-----------------|---------------------------------------------------|
| `nova`          | Tarefa detectada pela primeira vez.               |
| `lembrete_24h`  | Prazo entre `now+22h` e `now+26h`, ainda pendente.|
| `lembrete_2h`   | Prazo entre `now+1h` e `now+3h`, ainda pendente.  |

A janela larga absorve o jitter do poll a cada 15 min. Cada par
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
tarefa antes de orientar.

- `src/moodle/reader.py` — `obter_atividade` (enunciado/prazo/status/anexos),
  `obter_curso`, `listar_cursos`, `baixar_arquivo` (download autenticado via a
  mesma sessão Playwright + extração de texto de PDF/DOCX/PPTX/HTML).
- `src/agent/tools.py` — expõe essas funções como ferramentas (somente-leitura),
  com budget de chamadas e cache.
- `src/agent/orientador.py` — loop manual de tool-calls e o roteiro final.

Coleta **híbrida**: a cada poll, tarefas novas têm o enunciado completo lido e
salvo automaticamente; o download pesado de materiais só roda quando você pede
**Orientar**. Limites em `src/config.py` (`agent_max_iterations`,
`agent_max_tool_calls`, `reader_max_download_bytes`, etc.).

> Todas as ferramentas do agente são **somente-leitura** — nunca escrevem no
> Moodle.

## Próximas fases (não implementadas)

- Migração de SQLite para SQL Server.
