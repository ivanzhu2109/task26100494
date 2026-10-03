"""Facade combining roster and issue capabilities for one publisher."""

from .db import connect
from .issues import IssueService
from .roster import Roster


class NewsletterService:
    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)
        self._roster = Roster(self._conn)
        self._issues = IssueService(self._conn)

    @property
    def roster(self) -> Roster:
        return self._roster

    @property
    def issues(self) -> IssueService:
        return self._issues

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NewsletterService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
