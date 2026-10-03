"""Offline newsletter audience sealing and dispatch confirmation library."""

from .issues import IssueResult, TargetResult
from .roster import RosterError
from .service import NewsletterService

__all__ = ["NewsletterService", "IssueResult", "TargetResult", "RosterError"]
