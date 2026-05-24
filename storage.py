"""
Hermes — Stockage persistant SQLite
Remplace le dictionnaire RAM sessions : {user_id: {client, history}}
Inspiré de hermes-agent-main/hermes_state.py (Nous Research, MIT)
"""

import sqlite3
import threading
import json
import time
import random
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "hermes.db"

# ── Schéma ────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    user_id     INTEGER NOT NULL,
    client      TEXT    NOT NULL DEFAULT '_global',
    started_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    role        TEXT    NOT NULL,  -- 'user' | 'model'
    content     TEXT    NOT NULL,
    ts          TEXT    NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""

# ── Connexion thread-safe ─────────────────────────────────────────────────────
_lock = threading.Lock()

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)

def _write(sql: str, params: tuple = ()):
    """Écriture avec retry exponentiel (inspiré de hermes_state.py)."""
    for attempt in range(5):
        try:
            with _lock:
                with _connect() as conn:
                    conn.execute(sql, params)
                    conn.commit()
                    return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                time.sleep(random.uniform(0.02, 0.15) * (2 ** attempt))
            else:
                raise

# ── API sessions ──────────────────────────────────────────────────────────────

def get_session(user_id: int) -> dict:
    """Retourne {client, history} depuis la DB. Crée si absent."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT client FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            now = datetime.now().isoformat()
            _write(
                "INSERT INTO sessions (user_id, client, started_at, updated_at) VALUES (?,?,?,?)",
                (user_id, "_global", now, now)
            )
            client = "_global"
        else:
            client = row["client"]

        # Charger les 20 derniers messages
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (user_id,)
        ).fetchall()
        history = [{"role": r["role"], "parts": [r["content"]]} for r in reversed(rows)]

    return {"client": client, "history": history}

def set_client(user_id: int, client: str):
    now = datetime.now().isoformat()
    _write(
        "INSERT INTO sessions (user_id, client, started_at, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET client=excluded.client, updated_at=excluded.updated_at",
        (user_id, client, now, now)
    )

def add_message(user_id: int, role: str, content: str):
    _write(
        "INSERT INTO messages (user_id, role, content, ts) VALUES (?,?,?,?)",
        (user_id, role, content, datetime.now().isoformat())
    )
    # Mettre à jour updated_at
    _write(
        "UPDATE sessions SET updated_at=? WHERE user_id=?",
        (datetime.now().isoformat(), user_id)
    )

def reset_history(user_id: int):
    """Vide l'historique d'un user (reset session)."""
    _write("DELETE FROM messages WHERE user_id = ?", (user_id,))

def search_messages(query: str, user_id: int = None, limit: int = 5) -> list[dict]:
    """Recherche FTS5 dans les messages. Retourne [{role, content, ts}]."""
    with _connect() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT m.role, m.content, m.ts FROM messages m "
                "JOIN messages_fts f ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? AND m.user_id = ? "
                "ORDER BY rank LIMIT ?",
                (query, user_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.role, m.content, m.ts FROM messages m "
                "JOIN messages_fts f ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit)
            ).fetchall()
        return [dict(r) for r in rows]
