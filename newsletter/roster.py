"""Subscriber roster management with strictly increasing versions."""

import sqlite3

VALID_TIERS = ("free", "paid")


class RosterError(Exception):
    pass


class Roster:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def current_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'roster_version'"
        ).fetchone()
        return int(row[0])

    def _lookup(self, subscriber_id: str):
        return self._conn.execute(
            "SELECT tier, status FROM subscribers WHERE id = ?", (subscriber_id,)
        ).fetchone()

    def _apply(self, subscriber_id: str, tier: str, status: str) -> int:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            version = self.current_version() + 1
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'roster_version'", (version,)
            )
            self._conn.execute(
                "INSERT INTO subscribers (id, tier, status) VALUES (?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET tier = excluded.tier, "
                "status = excluded.status",
                (subscriber_id, tier, status),
            )
            self._conn.execute(
                "INSERT INTO roster_events (version, subscriber_id, tier, status) "
                "VALUES (?, ?, ?, ?)",
                (version, subscriber_id, tier, status),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return version

    def subscribe(self, subscriber_id: str, tier: str) -> int:
        if tier not in VALID_TIERS:
            raise RosterError(f"invalid tier: {tier!r}")
        row = self._lookup(subscriber_id)
        if row is not None and row[1] == "active" and row[0] == tier:
            return self.current_version()
        return self._apply(subscriber_id, tier, "active")

    def unsubscribe(self, subscriber_id: str) -> int:
        row = self._lookup(subscriber_id)
        if row is None:
            raise RosterError(f"unknown subscriber: {subscriber_id!r}")
        if row[1] == "unsubscribed":
            return self.current_version()
        return self._apply(subscriber_id, row[0], "unsubscribed")

    def set_tier(self, subscriber_id: str, tier: str) -> int:
        if tier not in VALID_TIERS:
            raise RosterError(f"invalid tier: {tier!r}")
        row = self._lookup(subscriber_id)
        if row is None:
            raise RosterError(f"unknown subscriber: {subscriber_id!r}")
        if row[0] == tier:
            return self.current_version()
        return self._apply(subscriber_id, tier, row[1])
