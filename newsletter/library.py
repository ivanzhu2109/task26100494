"""核心库：订阅者名册、刊期封版、派发推进、结果查询。

数据与确认正文均存储在 SQLite 中，派发仅指本地 sent-marked 记录。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class Tier(str, Enum):
    FREE = "free"
    PAID = "paid"
    ALL = "all"


class TargetStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class TargetResult:
    subscriber_id: str
    status: TargetStatus
    body: Optional[str]


@dataclass(frozen=True)
class IssueInfo:
    issue_id: str
    sealed_version: int
    completed: bool
    targets: tuple[TargetResult, ...]


# 正文生成回调：输入固定刊期内容与目标 ID，返回正文；可能抛异常。
BodyGenerator = Callable[[str, str], str]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roster_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    tier TEXT
);
CREATE TABLE IF NOT EXISTS subscribers (
    subscriber_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    target_tier TEXT NOT NULL,
    sealed_version INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS issue_targets (
    issue_id TEXT NOT NULL,
    subscriber_id TEXT NOT NULL,
    status TEXT NOT NULL,
    body TEXT,
    PRIMARY KEY (issue_id, subscriber_id)
);
"""


class NewsletterLibrary:
    """单出版者的封版与派发确认库。由上层直接调用。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NewsletterLibrary":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 订阅者变更 ----

    def _record_change(self, subscriber_id: str, kind: str, tier: Optional[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO roster_events (subscriber_id, kind, tier) VALUES (?, ?, ?)",
            (subscriber_id, kind, tier),
        )
        return int(cur.lastrowid)

    def subscribe(self, subscriber_id: str, tier: Tier) -> int:
        """新增或重新订阅（沿用原 ID），返回严格递增的版本号。"""
        tier = Tier(tier)
        if tier is Tier.ALL:
            raise ValueError("订阅者等级必须是 free 或 paid")
        cur = self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT active FROM subscribers WHERE subscriber_id = ?",
                (subscriber_id,),
            ).fetchone()
            if row is not None and row[0]:
                raise ValueError(f"订阅者 {subscriber_id!r} 已处于活跃状态")
            version = self._record_change(subscriber_id, "subscribe", tier.value)
            self._conn.execute(
                "INSERT INTO subscribers (subscriber_id, tier, active) VALUES (?, ?, 1) "
                "ON CONFLICT(subscriber_id) DO UPDATE SET tier = excluded.tier, active = 1",
                (subscriber_id, tier.value),
            )
            self._conn.commit()
            return version
        except Exception:
            self._conn.rollback()
            raise

    def unsubscribe(self, subscriber_id: str) -> int:
        """退订，返回严格递增的版本号。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT active FROM subscribers WHERE subscriber_id = ?",
                (subscriber_id,),
            ).fetchone()
            if row is None or not row[0]:
                raise ValueError(f"订阅者 {subscriber_id!r} 不存在或已退订")
            version = self._record_change(subscriber_id, "unsubscribe", None)
            self._conn.execute(
                "UPDATE subscribers SET active = 0 WHERE subscriber_id = ?",
                (subscriber_id,),
            )
            self._conn.commit()
            return version
        except Exception:
            self._conn.rollback()
            raise

    def set_tier(self, subscriber_id: str, tier: Tier) -> int:
        """调整付费等级，返回严格递增的版本号。"""
        tier = Tier(tier)
        if tier is Tier.ALL:
            raise ValueError("订阅者等级必须是 free 或 paid")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT active FROM subscribers WHERE subscriber_id = ?",
                (subscriber_id,),
            ).fetchone()
            if row is None or not row[0]:
                raise ValueError(f"订阅者 {subscriber_id!r} 不存在或已退订")
            version = self._record_change(subscriber_id, "set_tier", tier.value)
            self._conn.execute(
                "UPDATE subscribers SET tier = ? WHERE subscriber_id = ?",
                (tier.value, subscriber_id),
            )
            self._conn.commit()
            return version
        except Exception:
            self._conn.rollback()
            raise

    # ---- 刊期封版 ----

    def seal_issue(self, issue_id: str, content: str, target_tier: Tier) -> int:
        """封版：选定调用期间的一个名册版本，冻结该版本下全部符合条件的目标。

        在单个 IMMEDIATE 事务内读取版本并快照目标，保证目标恰好是所选版本下
        活跃且满足等级条件的全部订阅者，不混合多个版本的记录。
        """
        target_tier = Tier(target_tier)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self._conn.execute(
                "SELECT 1 FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            if exists:
                raise ValueError(f"刊期 {issue_id!r} 已存在")
            row = self._conn.execute("SELECT MAX(seq) FROM roster_events").fetchone()
            sealed_version = int(row[0]) if row[0] is not None else 0
            if target_tier is Tier.ALL:
                rows = self._conn.execute(
                    "SELECT subscriber_id FROM subscribers WHERE active = 1 ORDER BY subscriber_id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT subscriber_id FROM subscribers WHERE active = 1 AND tier = ? "
                    "ORDER BY subscriber_id",
                    (target_tier.value,),
                ).fetchall()
            self._conn.execute(
                "INSERT INTO issues (issue_id, content, target_tier, sealed_version, completed) "
                "VALUES (?, ?, ?, ?, 0)",
                (issue_id, content, target_tier.value, sealed_version),
            )
            self._conn.executemany(
                "INSERT INTO issue_targets (issue_id, subscriber_id, status) VALUES (?, ?, ?)",
                [(issue_id, r[0], TargetStatus.PENDING.value) for r in rows],
            )
            self._conn.commit()
            return sealed_version
        except Exception:
            self._conn.rollback()
            raise

    # ---- 推进派发 ----

    def _is_suppressed(self, issue_id: str, subscriber_id: str) -> bool:
        """封版边界之后发生过退订，则本期永久抑制该目标。"""
        row = self._conn.execute(
            "SELECT sealed_version FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        sealed_version = int(row[0])
        hit = self._conn.execute(
            "SELECT 1 FROM roster_events WHERE subscriber_id = ? AND kind = 'unsubscribe' "
            "AND seq > ? LIMIT 1",
            (subscriber_id, sealed_version),
        ).fetchone()
        return hit is not None

    def _mark(self, issue_id: str, subscriber_id: str, status: TargetStatus,
              body: Optional[str] = None) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE issue_targets SET status = ?, body = ? "
                "WHERE issue_id = ? AND subscriber_id = ? AND status = ?",
                (status.value, body, issue_id, subscriber_id, TargetStatus.PENDING.value),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _maybe_complete(self, issue_id: str) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM issue_targets WHERE issue_id = ? AND status = ?",
                (issue_id, TargetStatus.PENDING.value),
            ).fetchone()
            if row[0] == 0:
                self._conn.execute(
                    "UPDATE issues SET completed = 1 WHERE issue_id = ?", (issue_id,)
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def advance(self, issue_id: str, generator: BodyGenerator) -> None:
        """推进派发：抑制封版后退订的目标，为其余目标生成并确认正文。

        生成异常的目标保持 pending，可在后续推进中重试；全部目标进入
        confirmed/suppressed 终态后刊期完成，之后推进不再改变结果。
        """
        row = self._conn.execute(
            "SELECT content, completed FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"刊期 {issue_id!r} 不存在")
        content, completed = row[0], row[1]
        if completed:
            return
        pending = [
            r[0]
            for r in self._conn.execute(
                "SELECT subscriber_id FROM issue_targets WHERE issue_id = ? AND status = ? "
                "ORDER BY subscriber_id",
                (issue_id, TargetStatus.PENDING.value),
            ).fetchall()
        ]
        for subscriber_id in pending:
            if self._is_suppressed(issue_id, subscriber_id):
                self._mark(issue_id, subscriber_id, TargetStatus.SUPPRESSED)
                continue
            try:
                body = generator(content, subscriber_id)
            except Exception:
                continue  # 生成异常：不产生确认，保持 pending 供后续重试
            # 生成期间可能发生退订：迟到的正文不得进入确认结果。
            if self._is_suppressed(issue_id, subscriber_id):
                self._mark(issue_id, subscriber_id, TargetStatus.SUPPRESSED)
                continue
            self._mark(issue_id, subscriber_id, TargetStatus.CONFIRMED, body)
        self._maybe_complete(issue_id)

    # ---- 读取刊期结果 ----

    def get_issue(self, issue_id: str) -> IssueInfo:
        """查询封版目标、各目标状态与已确认正文。"""
        row = self._conn.execute(
            "SELECT sealed_version, completed FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"刊期 {issue_id!r} 不存在")
        targets = tuple(
            TargetResult(
                subscriber_id=r[0],
                status=TargetStatus(r[1]),
                body=r[2] if r[1] == TargetStatus.CONFIRMED.value else None,
            )
            for r in self._conn.execute(
                "SELECT subscriber_id, status, body FROM issue_targets "
                "WHERE issue_id = ? ORDER BY subscriber_id",
                (issue_id,),
            ).fetchall()
        )
        return IssueInfo(
            issue_id=issue_id,
            sealed_version=int(row[0]),
            completed=bool(row[1]),
            targets=targets,
        )
