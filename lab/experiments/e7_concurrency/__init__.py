"""Concurrent writers on Iceberg: the op log every writer keeps and the oracle that grades the final tables.

The semantics (docs/research/e7-concurrent-writers.md) are commutative, so the order of commits never changes the
correct result: each op is one unique event `writer:seq` adding `delta` to `key`, and the correct final state is the
per-key sum over the ops that committed.
"""
