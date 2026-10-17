"""Writes to Iceberg tables through Trino that are safe to retry.

A write is safe to retry when two things hold:

- the statement is idempotent by an op id (`statements`): applying it a second time changes nothing;
- two attempts of one write never run at the same time (`retry.write`): before another attempt, the previous one is
  cancelled and waited on until Trino reports it finished or failed.

Then a retry after an ambiguous error (a timeout, a lost connection, a commit whose state is unknown) applies the
write at most once. The caller's connection must make one HTTP attempt per request (`max_attempts=1`): the trino
client's own retry re-sends a statement without knowing whether the first one is running.
"""

from .errors import error_class, is_fatal
from .retry import RetryPolicy, WriteResult, write
from .statements import add_once, insert_if_absent

__all__ = ["RetryPolicy", "WriteResult", "add_once", "error_class", "insert_if_absent", "is_fatal", "write"]
