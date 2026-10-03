"""Issue sealing, dispatch advancement, and result queries."""

import sqlite3
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

ContentGenerator = Callable[[str, str], str]


class IssueError(Exception):
    pass


@dataclass(frozen=True)
class TargetResult:
    subscriber_id: str
    status: str
    body: Optional[str]


@dataclass(frozen=True)
class IssueResult:
    issue_id: int
    sealed_version: int
    status: str
    targets: List[TargetResult]


class IssueService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _get_issue(self, issue_id: int):
        return self._conn.execute(
            "SELECT id, content, target_tier, sealed_version, status "
            "FROM issues WHERE id = ?",
            (issue_id,),
        ).fetchone()

    def seal(self, content: str, target_tier: str) -> int:
        if target_tier not in ("free", "paid"):
            raise IssueError(f"invalid target tier: {target_tier!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            version = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'roster_version'"
            ).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO issues (content, target_tier, sealed_version, status) "
                "VALUES (?, ?, ?, 'open')",
                (content, target_tier, version),
            )
            issue_id = int(cur.lastrowid)
            tier_filter = (
                "AND latest.tier = 'paid'" if target_tier == "paid" else ""
            )
            self._conn.execute(
                f"""
                INSERT INTO issue_targets (issue_id, subscriber_id, status)
                SELECT ?, latest.subscriber_id, 'pending'
                FROM roster_events latest
                WHERE latest.version = (
                    SELECT MAX(e.version) FROM roster_events e
                    WHERE e.subscriber_id = latest.subscriber_id
                      AND e.version <= ?
                )
                  AND latest.status = 'active'
                  {tier_filter}
                """,
                (issue_id, version),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return issue_id

    def _is_suppressed(self, issue_id: int, subscriber_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM roster_events e
            JOIN issues i ON i.id = ?
            WHERE e.subscriber_id = ?
              AND e.version > i.sealed_version
              AND e.status = 'unsubscribed'
            LIMIT 1
            """,
            (issue_id, subscriber_id),
        ).fetchone()
        return row is not None

    def _finalize_target(self, issue_id: int, subscriber_id: str, body: str) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self._is_suppressed(issue_id, subscriber_id):
                self._conn.execute(
                    "UPDATE issue_targets SET status = 'suppressed' "
                    "WHERE issue_id = ? AND subscriber_id = ? AND status = 'pending'",
                    (issue_id, subscriber_id),
                )
            else:
                self._conn.execute(
                    "UPDATE issue_targets SET status = 'confirmed', body = ? "
                    "WHERE issue_id = ? AND subscriber_id = ? AND status = 'pending'",
                    (body, issue_id, subscriber_id),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _maybe_complete(self, issue_id: int) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM issue_targets "
                "WHERE issue_id = ? AND status = 'pending'",
                (issue_id,),
            ).fetchone()[0]
            if pending == 0:
                self._conn.execute(
                    "UPDATE issues SET status = 'complete' WHERE id = ?", (issue_id,)
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def advance(self, issue_id: int, generator: ContentGenerator) -> None:
        issue = self._get_issue(issue_id)
        if issue is None:
            raise IssueError(f"unknown issue: {issue_id!r}")
        if issue[4] == "complete":
            return
        content = issue[1]
        pending = self._conn.execute(
            "SELECT subscriber_id FROM issue_targets "
            "WHERE issue_id = ? AND status = 'pending'",
            (issue_id,),
        ).fetchall()
        for (subscriber_id,) in pending:
            if self._is_suppressed(issue_id, subscriber_id):
                self._conn.execute(
                    "UPDATE issue_targets SET status = 'suppressed' "
                    "WHERE issue_id = ? AND subscriber_id = ? AND status = 'pending'",
                    (issue_id, subscriber_id),
                )
                continue
            try:
                body = generator(content, subscriber_id)
            except Exception:
                continue
            self._finalize_target(issue_id, subscriber_id, body)
        self._maybe_complete(issue_id)

    def result(self, issue_id: int) -> IssueResult:
        issue = self._get_issue(issue_id)
        if issue is None:
            raise IssueError(f"unknown issue: {issue_id!r}")
        rows = self._conn.execute(
            "SELECT subscriber_id, status, body FROM issue_targets "
            "WHERE issue_id = ? ORDER BY subscriber_id",
            (issue_id,),
        ).fetchall()
        targets = [
            TargetResult(
                subscriber_id=row[0],
                status=row[1],
                body=row[2] if row[1] == "confirmed" else None,
            )
            for row in rows
        ]
        return IssueResult(
            issue_id=issue[0],
            sealed_version=issue[3],
            status=issue[4],
            targets=targets,
        )
