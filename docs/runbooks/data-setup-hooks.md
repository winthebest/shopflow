# Runbook: re-running the data components' setup hooks

| | |
|---|---|
| Applies to | Argo CD Sync/PostSync hooks of `iceberg-catalog` (`polaris-db-copy`, `polaris-bootstrap`, `polaris-setup`), `trino` (`trino-pg-copy`, `trino-bronze-tables`), `airflow` (`airflow-secret-copy`, migrations, create-user), `flink` (`flink-secret-copy`, which also writes the Grafana datasource Secret, `flink-serving-ddl`) |
| Symptom | A setup Job failed or never ran, the app shows `Synced` (sometimes `Degraded`), and pushing a fix to the Job changes nothing |
| Seen | 2026-10-10, P4 slot on `k3d-sf-data` |

## Why it happens

- Hooks are not part of an app's diff. If only a hook Job's spec changes (labels, an init container), the app stays
  `Synced`, auto-sync does not start a new operation, and the fixed Job never runs. A change to a non-hook input
  (for example a script in a `configMapGenerator`, whose name carries a content hash) does trigger a sync.
- While a sync operation waits for a hook ("waiting for completion of hook batch/Job/..."), deleting that Job by hand
  deadlocks it: Argo keeps the `argocd.argoproj.io/hook-finalizer` on the Job, the Job stays `Terminating`, and the
  operation keeps waiting.
- A failing hook with `backoffLimit: 6` holds the operation for about 10 minutes of back-off before it fails.

## Procedure (in this order)

Set `K="kubectl --context k3d-<cluster>"` and `APP=<application>`.

1. Push the fix and hard-refresh the app:
   `$K -n argocd annotate application $APP argocd.argoproj.io/refresh=hard --overwrite`.
2. If an operation is still running on the old revision (`$K -n argocd get application $APP -o
   jsonpath='{.status.operationState.phase}'` is `Running`), terminate it:
   `$K -n argocd patch application $APP --type merge -p '{"status":{"operationState":{"phase":"Terminating"}}}'`.
3. Only for a hook Job stuck in `Terminating`: remove Argo's hook finalizer from that Job (and nothing else):
   `$K -n <namespace> patch job <job> --type json -p '[{"op":"remove","path":"/metadata/finalizers"}]'`.
4. Start a sync that runs the hooks:
   `$K -n argocd patch application $APP --type merge -p '{"operation":{"initiatedBy":{"username":"<lane>"},"sync":{"syncStrategy":{"hook":{}}}}}'`.
   The hooks run in wave order (copy Jobs first); `BeforeHookCreation` replaces the old Jobs.

Never delete a running hook Job to "restart" it: terminate the operation first (step 2), then sync (step 4).

## Avoiding it

- Fix a hook through an input that is part of the diff when possible (a generated ConfigMap), so auto-sync runs it.
- For changes to a hook Job's own spec, step 4 alone is enough when no operation is running: it is the "sync with
  hooks" button of the Argo CD UI, which needs no Argo CD login for the lane (ADR 0205: UI only through port-forward).

## Setup Jobs under NetworkPolicies: retry connections inside the pod

Seen 2026-10-10 on `sf-main`, where profile `data` applies `network-policies-data` from the first sync:
`polaris-setup` failed 6 of 6 times with `URLError [Errno 111] Connection refused` on `http://polaris:8181`, although
`allow-same-namespace` admits it. Without its Secrets, `trino` stayed in `CreateContainerConfigError` and the Iceberg
sink never became Ready. Clusters that got the policies only after setup did not show it.

- **Cause:** kube-router adds a new pod's IP to the allow-list (ipset) of an isolated server a few seconds after the
  pod starts. Until then the server side REJECTs the packets, which looks like `Connection refused`. A client that
  connects right at start fails, and a Job retry does not help: every retry is a new pod with a new IP, so it hits
  the same window.
- **Rule:** every short-lived client that connects to an isolated pod retries connection errors inside its own pod
  for about 60s before failing. HTTP and SQL errors keep failing at once, and the Job's `backoffLimit` stays for slow
  dependencies.

| Client | Connects to | Retry in the pod |
|---|---|---|
| `polaris-bootstrap`, init `create-schema` | `shop-db` | `psql` loop, 60s; the bootstrap container then shares the pod's admitted IP |
| `polaris-setup` | `polaris:8181` | `call()` retries `URLError`/`ConnectionError`/timeouts with backoff 1–8s for 60s (`CONNECT_DEADLINE_SECONDS`) |
| `trino-bronze-tables` | `trino:8443` | `sh` loop, 12 × 5s; every statement is `IF NOT EXISTS` |
| `*-secret-copy`, `polaris-db-copy`, `trino-pg-copy` | API server only | none needed: the API server is not a pod behind these policies |
| Airflow migrations, create-user | `shop-db` | none: Airflow's imports take about 10s before the first connection; recheck if they fail with `Connection refused` |

Long-running pods (Polaris, Trino, Kafka Connect, the exporter, the Airflow scheduler with LocalExecutor)
reconnect by themselves and are not affected.
