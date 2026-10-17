# 0418. Iceberg tags are short-lived pins; their max age counts from the snapshot, and no guarantee rests on them

- Status: Accepted
- Date: 2026-10-16
- Lane: sf-data

## Context

Later lab work wants to pin snapshots: a dry run clones into the sandbox, tags and rolls back; maintenance may want
to keep "the snapshot before" for a while. Two questions first: does a tag keep its snapshot from Trino's
`expire_snapshots`, and does a tag with `max_ref_age_ms` expire? Trino 483 reads refs (`$refs`, `FOR VERSION AS OF
'<tag>'`) but cannot create them (`ALTER TABLE … CREATE TAG` is a syntax error; `EXECUTE create_tag` gives `Table
procedure not registered: CREATE_TAG`), so the tags were created with PyIceberg 0.12.0 through Polaris.

Method (`lab/spike/tag-probe.py`, 2026-10-16 14:43Z, catalog `lab_exp` with floors 0s, identity `harness`): CREATE +
4 INSERTs = snapshots s0 (empty) … s4 (main); 40 s later four tags, then `expire_snapshots(retention_threshold =>
'0s')` and `remove_orphan_files(retention_threshold => '0s')` right after the tagging. Every snapshot was 40.5–40.9 s
old at the expiry.

| Tag | Snapshot | `max_ref_age_ms` | After expire + remove_orphan_files | `SELECT count(*) … FOR VERSION AS OF` the tag |
|---|---|---|---|---|
| `keep_forever` | s1 | none | tag and snapshot kept | 1 (s1's rows) |
| `keep_1h` | s2 | 3 600 000 | tag and snapshot kept | 2 |
| `keep_30s` | s3 | 30 000 | tag removed, snapshot expired | `Cannot find snapshot with reference name: keep_30s` |
| `main_30s` | s4 (= main) | 30 000 | tag removed, snapshot kept by main | `Cannot find snapshot with reference name: main_30s` |

s0, which no ref pointed to, expired.

1. **A tag keeps its snapshot: yes**, and its files: after `remove_orphan_files` the tagged snapshots still read.
2. **A tag with `max_ref_age_ms` expires: yes**, at the next `expire_snapshots` once its *snapshot* is older than
   the max age. The age is not counted from the tagging: `keep_30s` was set moments before and still went, with its
   snapshot.

## Decision

- A tag is a short-lived pin owned by the operation that set it. To keep an old snapshot for another N, set
  `max_ref_age_ms` to the snapshot's age plus N (computed from its `committed_at`), or set none and remove the tag
  when done. A forgotten tag without a max age pins its files indefinitely.
- No recovery or retention guarantee rests on tags. Retention is what `expire_snapshots` thresholds and the
  floors keep (ADR 0417); the bytes refs pin are a measured feature of a table, and anything that creates tags must
  also clean them up.
- Tags are created through the REST catalog, by an identity whose principal may commit to the table. Checked the
  same day on `lab_evalprod`: the `lab_ops` principal created a tag, and `agent_planner`'s `lab_ro` principal was
  refused (`ForbiddenException: Principal 'lab_ro' … is not authorized for op SET_TABLE_SNAPSHOT_REF`).

## Alternatives considered

| Option | Why not |
|---|---|
| Branches as pins | A branch also carries history settings (minimum snapshots, maximum snapshot age) that a pin does not need; not measured |
| Table property `history.expire.max-ref-age-ms` | One default for every ref of a table; a pin needs its own lifetime |
| Tags as the retention mechanism | Pins without an owner leak storage; a max age counted from the snapshot cannot express "keep for another hour" without computing it |

## Consequences

- Positive: known semantics for the sandbox and maintenance designs, measured on the platform's Trino and Polaris.
- Negative / risks: tag creation needs a second client (PyIceberg or a REST call) next to Trino.
- When to revisit: Trino gains tag DDL; Iceberg changes how ref expiry is computed.

Reproduce: `lab/spike/run.sh` (runs the tag probe after the boundary probe; output in `out/lab-spike/…/tags.json`).
