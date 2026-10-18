"""Error classes, and which errors are not worth retrying."""

import pytest
import requests
from trino.exceptions import TrinoConnectionError, TrinoQueryError

from lakeops.writes import error_class, is_fatal


def query_error(name: str, error_type: str = "EXTERNAL", *causes: str) -> TrinoQueryError:
    info: dict | None = None
    for cause in reversed(causes):
        info = {"type": cause, "message": "…", "cause": info}
    return TrinoQueryError({"errorName": name, "errorType": error_type, "message": "failed", "failureInfo": info}, "q")


@pytest.mark.parametrize(
    ("error", "cls"),
    [
        (
            query_error("ICEBERG_COMMIT_ERROR", "EXTERNAL", "io.trino.spi.TrinoException", "x.CommitFailedException"),
            "ICEBERG_COMMIT_ERROR/x.CommitFailedException",
        ),
        (query_error("SYNTAX_ERROR", "USER_ERROR"), "SYNTAX_ERROR"),
        (requests.exceptions.ReadTimeout(), "client-timeout"),
        (requests.exceptions.ConnectTimeout(), "client-timeout"),
        (requests.exceptions.ConnectionError(), "connection-error"),
        (TrinoConnectionError("lost"), "connection-error"),
        (OSError("reset"), "OSError"),
    ],
)
def test_error_classes(error: BaseException, cls: str):
    assert error_class(error) == cls


def test_user_errors_are_fatal_except_conflicts():
    assert is_fatal(query_error("SYNTAX_ERROR", "USER_ERROR"))
    assert is_fatal(query_error("PERMISSION_DENIED", "USER_ERROR"))
    assert not is_fatal(query_error("TRANSACTION_CONFLICT", "USER_ERROR"))
    assert not is_fatal(query_error("ICEBERG_COMMIT_ERROR", "EXTERNAL"))
    assert not is_fatal(query_error("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR"))
    assert not is_fatal(requests.exceptions.ReadTimeout())
