"""scripts/restore-drill.sh against fake kubectl/curl and a k6 stand-in that writes an ack log."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from script_harness import REPO_ROOT, Harness

STAMP = "20261010t170000z"
OLD = "shop-db"
NEW = f"shop-db-drill-{STAMP}"
APPS = {
    "items": [
        {"metadata": {"name": name}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}
        for name in ("seaweedfs", "cnpg-barman-plugin", "shop-db", "shop")
    ]
}


def cluster(server: str) -> dict:
    return {
        "spec": {"plugins": [{"name": "barman-cloud.cloudnative-pg.io", "parameters": {"serverName": server}}]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "ContinuousArchiving", "status": "True"}]},
    }


def acks(n: int) -> str:
    """n acked orders, ids 1..n, one second apart, ending 10 seconds ago (before any disaster)."""
    end = datetime.now(UTC) - timedelta(seconds=10)
    lines = []
    for i in range(1, n + 1):
        at = end - timedelta(seconds=n - i)
        lines.append(json.dumps({"ack": {"order_id": i, "status": "paid", "acked_at": at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}}))
    return "\n".join(lines) + "\n"


@pytest.fixture
def drill(fake: Harness, tmp_path):
    """Root-app hook stand-in and a k6 stand-in; returns (env, hook record)."""
    record = tmp_path / "hook.log"
    hook = tmp_path / "platform-root-apps.sh"
    hook.write_text(f'#!/bin/sh\necho "KUBE_CONTEXT=$KUBE_CONTEXT $*" >> "{record}"\n')
    hook.chmod(0o755)
    ack_file = tmp_path / "acks.jsonl"
    ack_file.write_text(acks(10))
    k6 = fake.bin / "k6"
    k6.write_text(
        '#!/bin/sh\nout=""\nfor a in "$@"; do [ "$prev" = --console-output ] && out="$a"; prev="$a"; done\n'
        f'cat "{ack_file}" > "$out"\nexec sleep 30\n'
    )
    k6.chmod(0o755)
    fake.on("kubectl", r"config view --minify --flatten --context k3d-sf-main", "apiVersion: v1\n")
    fake.on("kubectl", r"-n argocd get applications.argoproj.io -o json", json_out=APPS)
    fake.on("kubectl", r"-n argocd get pods -l app.kubernetes.io/name=argocd-application-controller", "")
    fake.on("kubectl", r"-n shop get pods,pvc -l cnpg.io/cluster=shop-db -o name", "")
    fake.on("kubectl", r"get clusters.postgresql.cnpg.io shop-db$", exit=1, stderr="NotFound")
    fake.on("kubectl", r"cnpg.io/instanceRole=primary -o name", "pod/shop-db-1\n")
    fake.on("kubectl", r"exec pod/shop-db-1 -c postgres -- psql", "".join(f"{i}\n" for i in range(1, 8)))
    fake.on("curl", r"/products$", json_out=[{"id": 3}])
    fake.on("curl", r"-X POST .*/checkout", json_out={"id": 8, "status": "paid"})
    env = {"ROOT_APPS_HOOK": str(hook), "DRILL_STAMP": STAMP, "DRILL_K6_DRAIN": "0"}
    return env, record


def backups(fake: Harness, name: str) -> None:
    fake.on("kubectl", rf"backups.postgresql.cnpg.io {name} -o jsonpath", ["", "completed"])
    fake.on(
        "kubectl",
        rf"backups.postgresql.cnpg.io {name} -o json",
        json_out={"status": {"serverName": name.split("-base")[0], "backupId": "B1"}},
    )


def pointer(fake: Harness) -> dict:
    return json.loads((fake.tmp / "out" / "pointer.json").read_text())


def test_prepare_takes_a_base_backup_on_the_chain_make_up_started(fake, drill):
    env, record = drill
    fake.on("kubectl", r"clusters.postgresql.cnpg.io shop-db -o json", json_out=cluster(OLD))
    backups(fake, f"{OLD}-base-{STAMP}")

    result = fake.run("restore-drill.sh", "prepare", env=env)

    assert result.returncode == 0, result.stderr
    applied = next(c for c in fake.calls("kubectl") if "apply" in c.argv)
    assert applied.argv[-1].endswith(f"backup-{OLD}-base-{STAMP}.yaml")
    assert pointer(fake) == {"serverName": OLD, "backupId": "B1", "backup": f"{OLD}-base-{STAMP}"}
    assert not record.exists(), "prepare does not touch the root apps"
    assert not fake.calls("aws")


def test_prepare_refuses_a_cluster_without_a_backup_chain(fake, drill):
    env, _ = drill
    fake.on("kubectl", r"clusters.postgresql.cnpg.io shop-db -o json", json_out={**cluster(""), "spec": {}})

    result = fake.run("restore-drill.sh", "prepare", env=env)

    assert result.returncode != 0
    assert "has no backup chain" in result.stderr
    assert not fake.mutations()


def test_profile_drill_must_be_healthy(fake, drill):
    env, _ = drill
    fake.rules.insert(
        0, {"tool": "kubectl", "match": r"-n argocd get applications", "stdout": json.dumps({"items": APPS["items"][2:]}), "exit": 0}
    )

    result = fake.run("restore-drill.sh", "prepare", env=env)

    assert result.returncode != 0 and "profile drill is not Synced/Healthy" in result.stderr


def run_ready(fake: Harness) -> None:
    (fake.tmp / "out").mkdir(exist_ok=True)
    (fake.tmp / "out" / "pointer.json").write_text(json.dumps({"serverName": OLD, "backupId": "B0", "backup": f"{OLD}-base-1"}))
    fake.on("kubectl", rf"backups.postgresql.cnpg.io {OLD}-base-1 -o jsonpath", "completed")
    # preflight Ready, archiving to the old chain; after the recovery, the new chain.
    fake.on("kubectl", r"clusters.postgresql.cnpg.io shop-db -o json", [json.dumps(cluster(OLD))] * 2 + [json.dumps(cluster(NEW))])
    backups(fake, f"{NEW}-base")


def test_drill_destroys_recovers_and_measures(fake, drill):
    env, record = drill
    run_ready(fake)

    result = fake.run("restore-drill.sh", "run", "--warmup", "1", env=env)

    assert result.returncode == 0, result.stderr
    order = [
        r"scale statefulset argocd-application-controller --replicas=0",
        r"delete pod -l cnpg.io/cluster=shop-db --grace-period=0 --force",
        r"delete clusters.postgresql.cnpg.io shop-db",
        r"delete pvc -l cnpg.io/cluster=shop-db",
        r"scale statefulset argocd-application-controller --replicas=1",
        r"exec pod/shop-db-1 -c postgres -- psql",
        r"curl .*-X POST .*/checkout",
        rf"apply -f .*backup-{NEW}-base.yaml",
    ]
    positions = [fake.index_of(s) for s in order]
    assert positions == sorted(positions), list(zip(order, positions, strict=True))
    hook = record.read_text()
    expected = "KUBE_CONTEXT=k3d-sf-main --overlay local --revision main --profiles core,drill"
    assert f"{expected} --param pg.serverName={NEW} --param pg.recoveryFrom={OLD}" in hook
    assert "pg.recoveryTargetTime" not in hook

    out = json.loads((fake.tmp / "out" / f"run-{STAMP}" / "result.json").read_text())
    assert out["acked"] == 10 and out["lost"] == 3
    assert out["rpo_seconds"] == 3.0, "last ack (id 10) minus last surviving ack (id 7), both on the k6 clock"
    assert out["recovered_from"] == OLD and out["new_chain"] == NEW and out["pitr_mark"] is None
    assert out["rto_db_seconds"] >= 0 and out["rto_service_seconds"] >= out["rto_db_seconds"]
    assert pointer(fake)["serverName"] == NEW, "the next drill starts from the new chain"
    assert not fake.calls("aws")


def test_pitr_passes_the_mark_and_checks_both_sides(fake, drill):
    env, record = drill
    run_ready(fake)

    result = fake.run("restore-drill.sh", "run", "--warmup", "1", "--pitr", "14", env=env)

    assert result.returncode == 0, result.stderr
    hook = record.read_text()
    assert "--param pg.recoveryTargetTime=" in hook
    out = json.loads((fake.tmp / "out" / f"run-{STAMP}" / "result.json").read_text())
    assert out["pitr_mark"] and {"pitr_before_mark_lost", "pitr_after_mark_kept"} <= out.keys()


def test_a_failed_recovery_restarts_the_argo_controller(fake, drill, tmp_path):
    env, _ = drill
    run_ready(fake)
    broken = tmp_path / "broken-hook.sh"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)

    result = fake.run("restore-drill.sh", "run", "--warmup", "1", env={**env, "ROOT_APPS_HOOK": str(broken)})

    assert result.returncode != 0
    assert fake.index_of(r"--replicas=0") < fake.index_of(r"--replicas=1"), "the trap scales the controller back"
    assert "restarting the Argo CD application controller" in result.stderr


def test_dry_run_changes_nothing(fake, drill):
    env, record = drill
    run_ready(fake)

    result = fake.run("restore-drill.sh", "run", "--dry-run", env=env)

    assert result.returncode == 0, result.stderr
    assert not fake.mutations()
    assert not record.exists()
    assert "DRY-RUN: env KUBECONFIG=" in result.stderr and "pg.recoveryFrom=shop-db" in result.stderr


def test_profiles_must_include_drill(fake, drill):
    env, _ = drill

    result = fake.run("restore-drill.sh", "run", "--profiles", "core", env=env)

    assert result.returncode != 0 and "--profiles must include drill" in result.stderr
    assert not fake.calls()


def test_the_k6_script_still_logs_acks_in_the_shape_the_drill_reads():
    """sf-app owns loadtest/checkout.js; the drill parses its ack lines."""
    source = (REPO_ROOT / "loadtest" / "checkout.js").read_text()
    assert (
        "console.log(JSON.stringify({ ack: { order_id: order.id, status: order.status, acked_at: new Date().toISOString() } }))" in source
    )
