"""Error classes for Trino writes, and which errors make a retry pointless."""

import requests
from trino import exceptions as trino_errors


def _deepest_cause(failure_info: object) -> str | None:
    node, deepest = failure_info, None
    while isinstance(node, dict):
        deepest = node.get("type") or deepest
        node = node.get("cause")
    return deepest


def error_class(error: BaseException) -> str:
    """The Trino error name with the deepest cause in its failure info (e.g.
    `ICEBERG_COMMIT_ERROR/org.apache.iceberg.exceptions.CommitFailedException`), or a client-side class:
    `client-timeout`, `connection-error`, or the exception's type."""
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


# User errors that a later attempt can still pass: another transaction got there first.
RETRYABLE_USER_ERRORS = frozenset({"TRANSACTION_CONFLICT"})


def is_fatal(error: BaseException, retryable: frozenset[str] = RETRYABLE_USER_ERRORS) -> bool:
    """A user error (syntax, permission, a missing table) fails the same way every time, so it is not retried; a
    conflict is a user error a later attempt can pass."""
    return (
        isinstance(error, trino_errors.TrinoQueryError)
        and error.error_type == "USER_ERROR"
        and error.error_name not in retryable
    )
