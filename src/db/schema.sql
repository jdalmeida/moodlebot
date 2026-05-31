-- Schema do Moodlebot — single source of truth.
-- Carregado por src/db/conn.py:init_schema() via executescript().
-- Idempotente (CREATE TABLE IF NOT EXISTS).
-- Exceção: `rascunhos` é dropada antes de recriar (mudança de schema na fase 2).

CREATE TABLE IF NOT EXISTS tarefas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moodle_id TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    curso TEXT NOT NULL,
    titulo TEXT NOT NULL,
    url TEXT NOT NULL,
    prazo TEXT,
    descricao TEXT,
    status TEXT NOT NULL DEFAULT 'pendente',
    primeira_visualizacao TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    atualizado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tarefas_status ON tarefas(status);
CREATE INDEX IF NOT EXISTS idx_tarefas_prazo  ON tarefas(prazo);

CREATE TABLE IF NOT EXISTS notificacoes_enviadas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tarefa_id INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
    tipo_notif TEXT NOT NULL
        CHECK(tipo_notif IN ('nova','lembrete_24h','lembrete_2h')),
    enviado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    telegram_chat_id INTEGER,
    telegram_message_id INTEGER,
    UNIQUE(tarefa_id, tipo_notif)
);

-- Fase 2: schema novo, incompatível com o anterior. Dropamos a tabela antiga
-- antes de criar — não havia rascunhos persistidos (fase 1 não gerava nenhum).
DROP TABLE IF EXISTS rascunhos;

CREATE TABLE IF NOT EXISTS rascunhos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tarefa_id       INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
    chat_id         INTEGER NOT NULL,
    versao          INTEGER NOT NULL,
    conteudo        TEXT NOT NULL,
    prompt_usado    TEXT,
    salvo_no_moodle INTEGER NOT NULL DEFAULT 0,
    criado_em       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(tarefa_id, chat_id, versao)
);
CREATE INDEX IF NOT EXISTS idx_rascunhos_tarefa_versao
    ON rascunhos(tarefa_id, chat_id, versao DESC);

CREATE TABLE IF NOT EXISTS conversas (
    chat_id        INTEGER NOT NULL,
    tarefa_id      INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
    estado         TEXT NOT NULL DEFAULT 'idle'
        CHECK(estado IN ('idle','coletando_contexto','revisando_rascunho')),
    contexto_json  TEXT,
    atualizado_em  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (chat_id, tarefa_id)
);
CREATE INDEX IF NOT EXISTS idx_conversas_chat_estado
    ON conversas(chat_id, estado);
