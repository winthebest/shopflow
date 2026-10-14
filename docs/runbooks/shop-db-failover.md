# Runbook: shop-db's primary changed

| | |
|---|---|
| Alert | `PostgresPrimaryChanged` (ticket) |
| Rule | [`deploy/platform/slo/base/shop-db-replication.prometheusrule.yaml`](../../deploy/platform/slo/base/shop-db-replication.prometheusrule.yaml) (tests: `slo/tests/shop-db-replication.test.yaml`) |
| Related | [shop-db-standby.md](shop-db-standby.md), [cdc-lag.md](cdc-lag.md), [cdc-resnapshot.md](cdc-resnapshot.md), [gameday.md](gameday.md) scenario 2 |

## What it means

A shop-db instance that was a standby now runs as the primary: CloudNativePG promoted it. The cause is a failover
(the old primary died or became unreachable), a switchover (a planned move, e.g. a rolling update), or a restore
drill recreating the cluster. The ticket names the new primary and stays up for 30 minutes.

What a failover costs, as measured on a local cluster with two instances (game day 2):
- **Writes:** checkouts fail for ~25 s, until the app reconnects through `shop-db-rw`. Expect the checkout burn
  tickets.
- **Data:** replication is asynchronous, so a commit the standby had not received is lost. None were lost at 10
  checkouts/s, but nothing guarantees that.
- **CDC:** with logical slot sync (the default for 2+ instances), Debezium resumes on the new primary's copy of
  `debezium_shop` with no gap. Without it, Debezium recreates the slot and snapshots again, and writes between the
  promotion and the new slot never reach bronze.
- **A false CDC page is possible:** the freshness probe reads through Polaris, whose catalog database is on shop-db, so
  a probe during the outage fails. On a cluster up for less than about an hour, that one minute can page
  `CdcLagBurn`.

## Check

```sh
K="kubectl --context k3d-<cluster> -n shop"
$K get cluster shop-db -o jsonpath='{.status.phase}{"  primary="}{.status.currentPrimary}{" since "}{.status.currentPrimaryTimestamp}{"  ready="}{.status.readyInstances}{"/"}{.status.instances}{"\n"}'
$K get events --field-selector involvedObject.name=shop-db --sort-by=.lastTimestamp | tail -20
$K get pods -l cnpg.io/cluster=shop-db -L cnpg.io/instanceRole
```

1. **Why the old primary went away:** its pod's last state (`$K describe pod <old-primary>`: OOMKilled, evicted,
   node gone), and the operator's events above. A switchover or a restore drill that you started needs nothing more.
2. **The old primary is back as a standby:** otherwise `PostgresStandbyNotStreaming` follows within 5 minutes (see
   [shop-db-standby.md](shop-db-standby.md)).
3. **CDC resumed on the synced slot:**
   ```sh
   kubectl --context k3d-<cluster> -n kafka logs -l strimzi.io/cluster=cdc,strimzi.io/kind=KafkaConnect --since=1h \
     | grep -E 'Obtained valid replication slot|no longer available|Snapshot ended|CREATE_REPLICATION_SLOT'
   ```
   "Obtained valid replication slot" followed by a skipped snapshot is the good case. "no longer available" or
   `CREATE_REPLICATION_SLOT` means the slot was recreated: run the gap check over the promotion ±15 minutes.
   ```sh
   scripts/sre-cdc-gap.sh --context k3d-<cluster> --from <promotion − 15m, RFC 3339 UTC> --to <promotion + 15m>
   ```
   It compares orders, payments and shipments in Postgres and bronze. A non-zero "only in Postgres" or "r without
   c" count calls for a re-snapshot into a new epoch ([cdc-resnapshot.md](cdc-resnapshot.md)).

## After

The ticket resolves 30 minutes after the promotion. The gate check's "CDC slot on the standby" line should pass
again once the old primary is back as a standby and holds a synced copy of `debezium_shop`.
