# Moodlebot

Agente autônomo que monitora tarefas pendentes em uma instalação Moodle 3.10.9
(UNISC) e notifica via Telegram. Esta fase entrega a **fundação**:

- Sessão Moodle persistida via `storage_state` do Playwright (login institucional
  feito uma única vez, manualmente).
- Scraping do dashboard para extrair tarefas pendentes.
- Bot Telegram com `/start`, `/ping`, `/tarefas`.
- Notificações `nova`, `lembrete_24h`, `lembrete_2h` com idempotência via SQLite.
- Scheduler (APScheduler) que faz poll periódico.

LLM e preenchimento automático de respostas **não** estão nesta fase — virão
depois que a coleta estiver validada.

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
│   ├── moodle/
│   │   ├── session.py       # Playwright + storage_state
│   │   └── scraper.py       # Parser do dashboard
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

| Comando    | Descrição                                                   |
|-----------|-------------------------------------------------------------|
| `/start`  | Mensagem de boas-vindas e lista de comandos.                |
| `/ping`   | Verifica se o bot está vivo. Responde `pong`.               |
| `/tarefas`| Lista tarefas pendentes atualmente conhecidas no banco.     |

Apenas IDs presentes em `TELEGRAM_ALLOWED_USER_IDS` recebem resposta — os
demais são silenciosamente ignorados.

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

## Próximas fases (não implementadas)

- Geração de rascunhos com LLM (Anthropic ou Gemini).
- Preenchimento automático de respostas no Moodle (sempre como **rascunho**,
  com aprovação humana via botões inline no Telegram).
- Migração de SQLite para SQL Server.
