"""Durable event ledger for polling, turn ownership, and GitHub effects."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ItemState:
    repo: str
    kind: str
    number: int
    thread_id: str | None
    last_updated_at: str
    last_comment_id: int


@dataclass(frozen=True)
class EventState:
    event_key: str
    operation_id: str
    repo: str
    kind: str
    number: int
    trigger_kind: str
    trigger_id: str
    status: str
    thread_id: str | None
    turn_id: str | None


class EventLedger:
    """Persist stable event identities and observable processing states."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def integrity_check(self) -> None:
        with self._connect() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise RuntimeError(f"github-watch 账本损坏: {row}")

    def recover_interrupted(self) -> dict[str, int]:
        """Recover only phases whose external effects provably have not started."""

        with self._connect() as connection:
            safe = connection.execute(
                """
                UPDATE events SET status = 'discovered', updated_at = ?
                WHERE status IN ('claimed', 'context_ready')
                """,
                (utc_now(),),
            ).rowcount
            uncertain = connection.execute(
                """
                UPDATE events SET status = 'manual_reconcile', updated_at = ?,
                                  error = 'runtime interrupted after external effect began'
                WHERE status IN (
                    'turn_running', 'turn_submitting', 'comment_posting'
                )
                """,
                (utc_now(),),
            ).rowcount
        return {"safe_requeued": safe, "manual_reconcile": uncertain}

    def has_baseline(self, repo: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (f"baseline:{repo}",)
            ).fetchone()
        return row is not None

    def establish_baseline(
        self,
        repo: str,
        items: list[tuple[str, int, str, int]],
    ) -> None:
        """Atomically mark every existing item without creating executable events."""

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM meta WHERE key = ?", (f"baseline:{repo}",)
            ).fetchone():
                return
            connection.executemany(
                """
                INSERT INTO items(
                    repo, kind, number, thread_id, last_updated_at,
                    last_comment_id, first_seen_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                [
                    (repo, kind, number, updated_at, comment_id, now, now)
                    for kind, number, updated_at, comment_id in items
                ],
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (f"baseline:{repo}", now),
            )

    def get_item(self, repo: str, kind: str, number: int) -> ItemState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT repo, kind, number, thread_id, last_updated_at, last_comment_id
                FROM items WHERE repo = ? AND kind = ? AND number = ?
                """,
                (repo, kind, number),
            ).fetchone()
        return ItemState(*row) if row is not None else None

    def insert_item(
        self,
        repo: str,
        kind: str,
        number: int,
        updated_at: str,
        last_comment_id: int,
    ) -> bool:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO items(
                    repo, kind, number, thread_id, last_updated_at,
                    last_comment_id, first_seen_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (repo, kind, number, updated_at, last_comment_id, now, now),
            )
        return cursor.rowcount == 1

    def observe_item(
        self,
        repo: str,
        kind: str,
        number: int,
        *,
        updated_at: str,
        last_comment_id: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE items
                SET last_updated_at = ?, last_comment_id = ?, updated_at = ?
                WHERE repo = ? AND kind = ? AND number = ?
                """,
                (updated_at, last_comment_id, utc_now(), repo, kind, number),
            )

    def set_thread(self, repo: str, kind: str, number: int, thread_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE items SET thread_id = ?, updated_at = ?
                WHERE repo = ? AND kind = ? AND number = ?
                """,
                (thread_id, utc_now(), repo, kind, number),
            )

    def create_event(
        self,
        *,
        event_key: str,
        repo: str,
        kind: str,
        number: int,
        trigger_kind: str,
        trigger_id: str,
    ) -> EventState | None:
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, event_key).hex
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_key, operation_id, repo, kind, number,
                    trigger_kind, trigger_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                """,
                (
                    event_key,
                    operation_id,
                    repo,
                    kind,
                    number,
                    trigger_kind,
                    trigger_id,
                    now,
                    now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self.get_event(event_key)

    def get_event(self, event_key: str) -> EventState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_key, operation_id, repo, kind, number,
                       trigger_kind, trigger_id, status, thread_id, turn_id
                FROM events WHERE event_key = ?
                """,
                (event_key,),
            ).fetchone()
        if row is None:
            raise KeyError(event_key)
        return EventState(*row)

    def pending_events(self) -> list[EventState]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_key, operation_id, repo, kind, number,
                       trigger_kind, trigger_id, status, thread_id, turn_id
                FROM events WHERE status = 'discovered'
                ORDER BY created_at, event_key
                """
            ).fetchall()
        return [EventState(*row) for row in rows]

    def transition(
        self,
        event_key: str,
        *,
        expected: tuple[str, ...],
        status: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        response: str | None = None,
        artifact_id: str | None = None,
        error: str | None = None,
    ) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        values: list[object] = [status, utc_now()]
        for column, value in (
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("response", response),
            ("artifact_id", artifact_id),
            ("error", error),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        placeholders = ",".join("?" for _ in expected)
        values.extend([event_key, *expected])
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE events SET {", ".join(assignments)}
                WHERE event_key = ? AND status IN ({placeholders})
                """,
                values,
            )
        if cursor.rowcount != 1:
            current = self.get_event(event_key)
            raise RuntimeError(
                f"事件状态转换冲突: {event_key} current={current.status} "
                f"expected={expected} next={status}"
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items(
                    repo TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('issue', 'pr')),
                    number INTEGER NOT NULL,
                    thread_id TEXT,
                    last_updated_at TEXT NOT NULL,
                    last_comment_id INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(repo, kind, number)
                );
                CREATE TABLE IF NOT EXISTS events(
                    event_key TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    repo TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('issue', 'pr')),
                    number INTEGER NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    thread_id TEXT,
                    turn_id TEXT,
                    response TEXT,
                    artifact_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(repo, kind, number)
                        REFERENCES items(repo, kind, number)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
