"""What an error tells the client about its op: definite-fail (nothing committed) or unknown (it may have).

Every error gets a class: the Trino error name with the deepest cause in its failure info (e.g.
`ICEBERG_COMMIT_ERROR/org.apache.iceberg.exceptions.CommitFailedException`), or a client-side class (`client-timeout`,
`connection-error`). A class is definite only if the arm's allowlist names it; everything else is unknown. The
allowlist starts empty and is filled from the conflict matrix on dev, before held-out.
"""

import requests
from trino import exceptions as trino_errors

from .oplog import Outcome


def _deepest_cause(failure_info: object) -> str | None:
    node, deepest = failure_info, None
    while isinstance(node, dict):
        deepest = node.get("type") or deepest
        node = node.get("cause")
    return deepest


def error_class(error: BaseException) -> str:
    if isinstance(error, trino_errors.TrinoQueryError):
        name = error.error_name or "QUERY_FAILED"
        cause = _deepest_cause(error.failure_info)
        return f"{name}/{cause}" if cause else name
    # requests' ConnectTimeout is both a Timeout and a ConnectionError: the timeout wins.
    if isinstance(error, requests.exceptions.Timeout):
        return "client-timeout"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "connection-error"
    return type(error).__name__


def classify(error: BaseException, definite: frozenset[str]) -> tuple[Outcome, str, str]:
    """The outcome, the error class and a short message for the op log."""
    cls = error_class(error)
    return (Outcome.FAILED if cls in definite else Outcome.UNKNOWN), cls, str(error)[:500]
