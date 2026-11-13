"""Repository scrapers.

Each scraper is fully isolated: it owns its own HTTP session, pacing rules and
error handling, and it can only communicate with the rest of the application
through :class:`FetchResult`. A failure in one upstream never propagates to the
other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..models import HarvestRecord

STATUS_OK = "ok"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass
class FetchResult:
    """Outcome of one repository harvest, success or failure."""

    source: str
    status: str = STATUS_OK
    records: List[HarvestRecord] = field(default_factory=list)
    total_available: int = 0
    pages_fetched: int = 0
    message: str = ""
    retry_after: int = 0

    @property
    def found(self) -> int:
        return len(self.records)

    def summary(self) -> dict:
        return {
            "source": self.source,
            "status": self.status,
            "found": self.found,
            "total_available": self.total_available,
            "pages_fetched": self.pages_fetched,
            "message": self.message,
            "retry_after": self.retry_after,
        }
