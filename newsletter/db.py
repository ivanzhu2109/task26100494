"""SQLite schema and connection helpers."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id TEXT PRIMARY KEY,
    tier TEXT NOT NULL CHECK (tier IN ('free', 'paid')),
    status TEXT NOT NULL CHECK (status IN ('active', 'unsubscribed'))
);

CREATE TABLE IF NOT EXISTS roster_events (
    version INTEGER NOT NULL,
    subscriber_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (version, subscriber_id)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    target_tier TEXT NOT NULL CHECK (target_tier IN ('free', 'paid')),
    sealed_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'complete'))
);

CREATE TABLE IF NOT EXISTS issue_targets (
    issue_id INTEGER NOT NULL REFERENCES issues (id),
    subscriber_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'suppressed')),
    body TEXT,
    PRIMARY KEY (issue_id, subscriber_id)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('roster_version', 0)"
    )
    return conn
