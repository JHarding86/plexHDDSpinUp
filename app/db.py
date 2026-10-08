"""SQLite storage: settings (JSON values), path maps, and the event log."""
import json
import secrets
import sqlite3
import threading
import time

EVENT_RETENTION_SECONDS = 30 * 24 * 3600

DEFAULTS = {
    "setup_complete": False,
    "ui_password_hash": "",
    "secret_key": "",
    "webhook_secret": "",
    "plex_client_id": "",
    "plex_account_token": "",
    "plex_url": "",
    "plex_token": "",
    "plex_server_name": "",
    "library_ids": [],            # empty = all libraries
    "events": ["media.play", "media.resume"],
    "allowed_users": [],          # empty = everyone
    "allowed_players": [],        # empty = every player
    "debounce_seconds": 120,
    "read_blocks": 3,
    "block_size": 4096,
    "cold_threshold_ms": 1000,
    "next_episode_enabled": True,
    "next_on_play": True,
    "next_episode_lookahead": 1,
    "websocket_enabled": False,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS path_maps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_prefix TEXT NOT NULL,
    local_prefix TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL,
    user TEXT, player TEXT, title TEXT,
    plex_path TEXT, local_path TEXT, disk TEXT,
    status TEXT NOT NULL,
    read_ms REAL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class DB:
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
        self._ensure_generated()

    def _ensure_generated(self):
        for key, gen in (
            ("secret_key", lambda: secrets.token_hex(32)),
            ("webhook_secret", lambda: secrets.token_urlsafe(18)),
            ("plex_client_id", lambda: "spinup-" + secrets.token_hex(8)),
        ):
            if not self.get(key):
                self.set(key, gen())

    # --- settings -------------------------------------------------------
    def get(self, key):
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULTS.get(key)
        return json.loads(row["value"])

    def set(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def settings(self):
        out = dict(DEFAULTS)
        with self._lock:
            for row in self._conn.execute("SELECT key, value FROM settings"):
                out[row["key"]] = json.loads(row["value"])
        return out

    # --- path maps ------------------------------------------------------
    def path_maps(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM path_maps ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def add_path_map(self, plex_prefix, local_prefix):
        with self._lock:
            self._conn.execute(
                "INSERT INTO path_maps(plex_prefix, local_prefix) VALUES(?, ?)",
                (plex_prefix, local_prefix),
            )

    def delete_path_map(self, map_id):
        with self._lock:
            self._conn.execute("DELETE FROM path_maps WHERE id=?", (map_id,))

    # --- events ---------------------------------------------------------
    def log_event(self, **fields):
        fields.setdefault("ts", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            self._conn.execute(f"INSERT INTO events({cols}) VALUES({marks})", tuple(fields.values()))
            self._conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - EVENT_RETENTION_SECONDS,))

    def recent_events(self, limit=50):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def has_event(self, trigger):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM events WHERE trigger LIKE ? LIMIT 1", (trigger,)).fetchone() is not None

    def stats_since(self, since_ts, cold_status="cold"):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(status = ?) AS cold,"
                " AVG(CASE WHEN status = ? THEN read_ms END) AS avg_cold_ms"
                " FROM events WHERE ts >= ? AND status IN ('cold', 'warm')",
                (cold_status, cold_status, since_ts),
            ).fetchone()
        return {"total": row["total"] or 0, "cold": row["cold"] or 0, "avg_cold_ms": row["avg_cold_ms"]}
