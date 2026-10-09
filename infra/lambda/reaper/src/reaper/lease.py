"""Session lease kept in SSM as an ISO 8601 UTC timestamp.

A missing or unreadable lease counts as expired: a forgotten cluster costs money, a reaped one
only costs a restore from the last backup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaseStatus:
    expired: bool
    expires_at: datetime | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "expired": self.expired,
            "expiresAt": self.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if self.expires_at else None,
            "reason": self.reason,
        }


def parse_lease(value: str) -> datetime:
    """Parse ``2026-10-09T14:00:00Z``; a timestamp without an offset is rejected."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        raise ValueError("lease timestamp has no UTC offset")
    return parsed.astimezone(UTC)


def read_lease(ssm, parameter_name: str, now: datetime) -> LeaseStatus:
    """Read the lease. Errors other than "not found" (for example AccessDenied) propagate."""
    try:
        value = ssm.get_parameter(Name=parameter_name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return LeaseStatus(expired=True, expires_at=None, reason="no lease")
    try:
        expires_at = parse_lease(value)
    except ValueError:
        log.warning("lease %s is unreadable; treating it as expired", parameter_name)
        return LeaseStatus(expired=True, expires_at=None, reason="unreadable lease")
    if now >= expires_at:
        return LeaseStatus(expired=True, expires_at=expires_at, reason="lease expired")
    return LeaseStatus(expired=False, expires_at=expires_at, reason="lease valid")
