"""After an operation commits, check the operation's own snapshot against its parent, by table kind and op class.

This belongs to the side that acts: the executor or guard, after a write through `lakeops.writes`. An eval scorer
must never call it. A scorer judges a run independently, from table state and the SQL log, so it can't share a
verdict with the code it grades.

Interface
---------
``verify(conn, table, *, query_id, op, kind, effect_bounds=None) -> Verification``

- ``table``: ``catalog.schema.table``.
- ``query_id``: the operation's Trino query id. Trino writes it into the summary of every snapshot it commits
  (``trino_query_id``), which is how the operation's snapshot is found. It is never compared with "the current
  snapshot", which may already hold other writers' commits.
- ``op``, the operation's class:
  - ``append``: INSERT. Nothing is deleted, and the rows added equal the snapshot's added records.
  - ``rewrite``: OPTIMIZE, optimize_manifests, compaction. The logical row count is unchanged.
  - ``dml``: DELETE, UPDATE, MERGE. The change in row count is within ``effect_bounds``.
- ``kind``: a ``TableKind``.
  - ``key_columns``: a keyed table must have no duplicate key at the operation's snapshot. An append-only table
    (bronze) has none, so duplicates are allowed there.
  - ``external_writer``: someone else also writes the table (e.g. the CDC sink), so a rollback could discard their
    commits.
- ``effect_bounds``: ``(min, max)`` change in row count allowed for a ``dml`` op, from the approved plan.

The ``Verification`` result:
- ``ok``: no finding;
- ``findings``: each failed check, with the numbers;
- ``snapshot_id`` / ``parent_id`` of the operation;
- ``rollback_safe`` and ``rollback_reason``. A rollback is safe only when the table's current snapshot is still the
  operation's own, the table has no external writer, and the op is not a ``rewrite`` (a rewrite doesn't change the
  logic and is never rolled back automatically).

Still to come: for a ``rewrite``, the logical checksum (before = after) from the shared ``lakeops.fingerprint``, once
it exists. There is one checksum implementation, not two.
"""

from .checks import Finding, TableKind, Verification, verify

__all__ = ["Finding", "TableKind", "Verification", "verify"]
