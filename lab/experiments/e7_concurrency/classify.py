"""What an error tells the client about its op: definite-fail (nothing committed) or unknown (it may have).

The error class comes from `lakeops.writes.error_class`: the Trino error name with the deepest cause in its failure
info (e.g. `ICEBERG_COMMIT_ERROR/org.apache.iceberg.exceptions.CommitFailedException`), or a client-side class
(`client-timeout`, `connection-error`). A class is definite only if the arm's allowlist names it; everything else is
unknown. The allowlist starts empty and is filled from the conflict matrix on dev, before held-out.
"""

from lakeops.writes import error_class

from .oplog import Outcome

__all__ = ["classify", "error_class"]


def classify(error: BaseException, definite: frozenset[str]) -> tuple[Outcome, str, str]:
    """The outcome, the error class and a short message for the op log."""
    cls = error_class(error)
    return (Outcome.FAILED if cls in definite else Outcome.UNKNOWN), cls, str(error)[:500]
