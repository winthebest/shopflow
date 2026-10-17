"""Error classes, and the rule that only an allowlisted class is a definite failure."""

import pytest
import requests
from e7_concurrency.classify import classify, error_class
from e7_concurrency.oplog import Outcome
from trino.exceptions import TrinoQueryError

COMMIT_FAILED = "ICEBERG_COMMIT_ERROR/org.apache.iceberg.exceptions.CommitFailedException"


def query_error(name: str | None, *causes: str) -> TrinoQueryError:
    info: dict | None = None
    for cause in reversed(causes):
        info = {"type": cause, "message": "…", "cause": info}
    return TrinoQueryError({"errorName": name, "errorType": "EXTERNAL", "message": "failed", "failureInfo": info}, "q1")


@pytest.mark.parametrize(
    ("error", "cls"),
    [
        (
            query_error("ICEBERG_COMMIT_ERROR", "io.trino.spi.TrinoException", COMMIT_FAILED.split("/")[1]),
            COMMIT_FAILED,
        ),
        (query_error("GENERIC_INTERNAL_ERROR"), "GENERIC_INTERNAL_ERROR"),
        (query_error(None), "QUERY_FAILED"),
        (requests.exceptions.ReadTimeout(), "client-timeout"),
        (requests.exceptions.ConnectTimeout(), "client-timeout"),
        (requests.exceptions.ConnectionError(), "connection-error"),
        (RuntimeError("boom"), "RuntimeError"),
    ],
)
def test_error_classes(error: BaseException, cls: str):
    assert error_class(error) == cls


def test_only_an_allowlisted_class_is_definite():
    error = query_error("ICEBERG_COMMIT_ERROR", "io.trino.spi.TrinoException", COMMIT_FAILED.split("/")[1])
    assert classify(error, frozenset())[:2] == (Outcome.UNKNOWN, COMMIT_FAILED)
    assert classify(error, frozenset({COMMIT_FAILED}))[:2] == (Outcome.FAILED, COMMIT_FAILED)
    unknown_commit = query_error("ICEBERG_COMMIT_ERROR", "org.apache.iceberg.exceptions.CommitStateUnknownException")
    assert classify(unknown_commit, frozenset({COMMIT_FAILED}))[0] is Outcome.UNKNOWN
    assert classify(requests.exceptions.ReadTimeout(), frozenset({"client-timeout"}))[0] is Outcome.FAILED
