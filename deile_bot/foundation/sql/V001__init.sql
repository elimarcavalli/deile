-- V001 — initial schema for deile_bot.sqlite

CREATE TABLE IF NOT EXISTS bot_user (
    bot_user_id      TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    display_name     TEXT NOT NULL,
    is_bot           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    UNIQUE (provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_user_last_seen ON bot_user(last_seen_at);

CREATE TABLE IF NOT EXISTS channel (
    provider             TEXT NOT NULL,
    provider_channel_id  TEXT NOT NULL,
    name                 TEXT,
    scope                TEXT NOT NULL,
    parent_channel_id    TEXT,
    PRIMARY KEY (provider, provider_channel_id)
);

CREATE TABLE IF NOT EXISTS message (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    provider             TEXT NOT NULL,
    provider_channel_id  TEXT NOT NULL,
    provider_message_id  TEXT NOT NULL,
    direction            TEXT NOT NULL,
    bot_user_id          TEXT NOT NULL REFERENCES bot_user(bot_user_id),
    text                 TEXT NOT NULL,
    reply_to_message_id  TEXT,
    sent_at              TEXT NOT NULL,
    persisted_at         TEXT NOT NULL,
    raw_json             TEXT,
    UNIQUE (provider, provider_channel_id, provider_message_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_message_channel_time
    ON message(provider, provider_channel_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_user_time
    ON message(bot_user_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS attachment (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    url              TEXT,
    mime             TEXT,
    filename         TEXT,
    size_bytes       INTEGER,
    bytes_inline_b64 TEXT
);

CREATE TABLE IF NOT EXISTS dlq (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    last_error    TEXT NOT NULL,
    attempts      INTEGER NOT NULL,
    enqueued_at   TEXT NOT NULL,
    next_retry_at TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type          TEXT NOT NULL,
    bot_user_id         TEXT,
    provider            TEXT,
    provider_channel_id TEXT,
    provider_message_id TEXT,
    payload_json        TEXT,
    occurred_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit(occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit(bot_user_id, occurred_at);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version(version, applied_at)
    VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
