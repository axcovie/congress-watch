"""SQLite storage. Telem returns each monitor result only once, so we keep them."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    topic_key   TEXT PRIMARY KEY,
    monitor_id  TEXT NOT NULL,
    query       TEXT NOT NULL,
    frequency   TEXT,
    status      TEXT,
    created_at  TEXT,
    expires_at  TEXT,
    webhook_secret TEXT
);
CREATE TABLE IF NOT EXISTS results (
    id          TEXT PRIMARY KEY,
    topic_key   TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT,
    summary     TEXT,
    publish_date TEXT,
    raw         TEXT,
    first_seen  TEXT NOT NULL,
    briefed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_unbriefed ON results(briefed_at, topic_key);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path = "congress_watch.db") -> None:
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # monitors -------------------------------------------------------------
    def save_monitor(self, topic_key: str, m: dict) -> None:
        self.db.execute(
            """INSERT INTO monitors(topic_key, monitor_id, query, frequency, status,
                                    created_at, expires_at, webhook_secret)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(topic_key) DO UPDATE SET
                 monitor_id=excluded.monitor_id, query=excluded.query,
                 frequency=excluded.frequency, status=excluded.status,
                 created_at=excluded.created_at, expires_at=excluded.expires_at,
                 webhook_secret=COALESCE(excluded.webhook_secret, monitors.webhook_secret)""",
            (
                topic_key,
                m["id"],
                m.get("query", ""),
                m.get("frequency"),
                m.get("status", "active"),
                m.get("created_at") or now_iso(),
                m.get("expires_at"),
                m.get("webhook_secret"),
            ),
        )
        self.db.commit()

    def set_monitor_status(self, topic_key: str, status: str) -> None:
        self.db.execute("UPDATE monitors SET status=? WHERE topic_key=?", (status, topic_key))
        self.db.commit()

    def monitors(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM monitors ORDER BY topic_key"))

    def monitor_by_id(self, monitor_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM monitors WHERE monitor_id=?", (monitor_id,)
        ).fetchone()

    def delete_monitor(self, topic_key: str) -> None:
        self.db.execute("DELETE FROM monitors WHERE topic_key=?", (topic_key,))
        self.db.commit()

    # results --------------------------------------------------------------
    def add_results(self, topic_key: str, results: list[dict]) -> int:
        added = 0
        for r in results:
            url = r.get("url")
            if not url:
                continue
            rid = hashlib.sha256(f"{topic_key}|{url}".encode()).hexdigest()[:24]
            cur = self.db.execute(
                """INSERT OR IGNORE INTO results(id, topic_key, url, title, summary,
                                                 publish_date, raw, first_seen)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    rid,
                    topic_key,
                    url,
                    r.get("title"),
                    r.get("summary") or r.get("excerpt") or r.get("content"),
                    r.get("publish_date"),
                    json.dumps(r, default=str),
                    now_iso(),
                ),
            )
            added += cur.rowcount
        self.db.commit()
        return added

    def unbriefed(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM results WHERE briefed_at IS NULL ORDER BY topic_key, first_seen"
            )
        )

    def mark_briefed(self, ids: list[str]) -> None:
        ts = now_iso()
        self.db.executemany("UPDATE results SET briefed_at=? WHERE id=?", [(ts, i) for i in ids])
        self.db.commit()

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM results ORDER BY first_seen DESC LIMIT ?", (limit,))
        )
