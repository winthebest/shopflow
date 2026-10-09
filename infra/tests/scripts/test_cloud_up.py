"""cloud-up: the backup chain fails closed, dry-run is read-only, a full run arms both kill switches."""

import base64
import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from script_harness import REPO_ROOT, Harness, operator

POINTER = {"serverName": "shop-db-20261001t080000z", "backupId": "20261001T110000"}
NODES = {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
APPS = {"items": [{"status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]}
CNPG = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
TRINO_PASSWORD = "trino-smoke-password-value"
GATEWAY_SVC = {"items": [{"status": {"loadBalancer": {"ingress": [{"hostname": "k8s-shop-abc.elb.ap-southeast-1.amazonaws.com"}]}}}]}


def preflight(h: Harness, *, lease=None, pointer=None, marker=None, epoch=None, cluster="ABSENT") -> Harness:
    operator(h)
    h.on("aws", r"budgets describe-budget-actions-for-budget", "STANDBY\n")
    h.on("aws", r"eks describe-cluster-versions", "STANDARD_SUPPORT\n")
    for name, value in (("lease-expires-at", lease), ("pg-backup-pointer", pointer), ("pg-initialized", marker)):
        if value is not None:
            h.on("aws", rf"ssm get-parameter --name /shopflow/aws/control/{name} ", value)
    if epoch is not None:
        h.on("aws", r"ssm get-parameter --name /shopflow/aws/kafka/cdc-epoch ", epoch)
    h.not_found("aws", r"ssm get-parameter", "ParameterNotFound", "GetParameter")
    if cluster == "ABSENT":
        h.not_found("aws", r"eks describe-cluster --name", "ResourceNotFoundException", "DescribeCluster")
    else:
        h.on("aws", r"eks describe-cluster --name", f"{cluster}\n")
    h.on("curl", r"checkip.amazonaws.com", "203.0.113.10\n")
    h.on("aws", r"ec2 describe-vpcs", "vpc-0123456789abcdef0\n")
    return h


@pytest.fixture
def hooks(tmp_path):
    """Stand-ins for the sf-platform root app hook and the sf-data epoch hook, plus the Argo CD chart files."""
    record = tmp_path / "hooks.log"
    paths = {}
    for name in ("platform-root-apps.sh", "cdc-epoch.sh"):
        hook = tmp_path / name
        hook.write_text(f'#!/bin/sh\necho "{name} KUBECONFIG=$KUBECONFIG $*" >> "{record}"\n')
        hook.chmod(0o755)
        paths[name] = hook
    chart = tmp_path / "argocd-chart.yaml"
    chart.write_text("repo: https://argoproj.github.io/argo-helm\nchart: argo-cd\nversion: 10.9.6\n")
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    env = {
        "ROOT_APPS_HOOK": str(paths["platform-root-apps.sh"]),
        "CDC_EPOCH_HOOK": str(paths["cdc-epoch.sh"]),
        "ARGOCD_CHART_FILE": str(chart),
        "ARGOCD_VALUES": str(values),
        "ARGOCD_AWS_VALUES": str(values),
    }
    return env, record


def test_marker_without_pointer_refuses_initdb(fake, hooks):
    preflight(fake, marker="20261001t080000z")

    result = fake.run("cloud-up.sh", env=hooks[0])

    assert result.returncode != 0
    assert "refusing initdb" in result.stderr
    assert fake.mutations() == []


def test_pointer_to_a_missing_backup_refuses_to_start(fake, hooks):
    preflight(fake, pointer=json.dumps(POINTER), marker="20261001t080000z")
    fake.not_found("aws", r"s3api head-object", "404", "HeadObject")

    result = fake.run("cloud-up.sh", env=hooks[0])

    assert result.returncode != 0
    assert "pg-backup/shop-db-20261001t080000z/base/20261001T110000/backup.info is missing" in result.stderr
    assert fake.mutations() == []


def test_active_lease_blocks_a_second_session(fake, hooks):
    preflight(fake, lease="2999-01-01T00:00:00Z")

    result = fake.run("cloud-up.sh", env=hooks[0])

    assert result.returncode != 0 and "a session is active" in result.stderr
    assert fake.mutations() == []


def test_fired_budget_action_blocks_the_session(fake, hooks):
    fake.on("aws", r"budgets describe-budget-actions-for-budget", "EXECUTION_SUCCESS\n")
    preflight(fake)

    result = fake.run("cloud-up.sh", env=hooks[0])

    assert result.returncode != 0 and "Budget Action has fired" in result.stderr


def test_extended_support_version_is_rejected(fake, hooks):
    fake.on("aws", r"eks describe-cluster-versions", "EXTENDED_SUPPORT\n")
    preflight(fake)

    result = fake.run("cloud-up.sh", env=hooks[0])

    assert result.returncode != 0 and "extended support costs" in result.stderr


def test_dry_run_plans_a_recovery_without_changing_anything(fake, hooks):
    env, record = hooks
    preflight(fake, pointer=json.dumps(POINTER), marker="20261001t080000z", epoch="7")

    result = fake.run("cloud-up.sh", "--dry-run", env=env)

    assert result.returncode == 0, result.stderr
    assert fake.mutations() == []
    plan = next(c for c in fake.calls("tofu") if "plan" in c.argv)
    assert "operator_cidr=203.0.113.10/32" in plan.argv
    calls = record.read_text().splitlines()
    assert [c.split()[-1] for c in calls if c.startswith("platform-root-apps.sh")] == ["--check", "--print"]
    root_apps = next(c for c in calls if c.endswith("--print"))
    for expected in (
        "--overlay aws",
        "pg.recoveryFrom=shop-db-20261001t080000z",
        "cdcEpoch=8",
        "operatorCidr=203.0.113.10/32",
        "aws.accountId=123456789012",
        "aws.vpcId=vpc-0123456789abcdef0",
    ):
        assert expected in root_apps


def full_session(h: Harness) -> Harness:
    preflight(h)
    h.on("kubectl", r"get nodes -o json", json_out=NODES)
    h.on("kubectl", r"-n argocd get applications.argoproj.io -o json", json_out=APPS)
    h.on("kubectl", r"clusters.postgresql.cnpg.io shop-db -o json", json_out=CNPG)
    h.on("kubectl", r"backups.postgresql.cnpg.io shop-db-\S+-initial -o jsonpath", ["", "completed"])
    h.on(
        "kubectl",
        r"backups.postgresql.cnpg.io shop-db-\S+-initial -o json",
        json_out={"status": {"serverName": "shop-db-new", "backupId": "B1"}},
    )
    h.on("kubectl", r"-n envoy-gateway-system get svc", json_out=GATEWAY_SVC)
    h.on("curl", r"-X POST .*/checkout", json_out={"id": 42, "status": "paid"})
    h.on("curl", r"/products$", json_out=[{"id": 3}])
    h.on(
        "kubectl",
        r"-n lakehouse get secret trino-exporter -o json",
        json_out={"data": {"password": base64.b64encode(TRINO_PASSWORD.encode()).decode()}},
    )
    h.on(
        "kubectl", r"exec -i deploy/trino-coordinator .*--user .*exporter .*lake_ro .*bronze.orders WHERE id = 42 AND _cdc_epoch = 1", "1\n"
    )
    h.not_found("aws", r"scheduler get-schedule", "ResourceNotFoundException", "GetSchedule")
    return h


def test_first_session_initdb_end_to_end(fake, hooks):
    env, record = hooks
    full_session(fake)

    result = fake.run("cloud-up.sh", "--hours", "3", env=env)

    assert result.returncode == 0, result.stderr
    steps = [
        r"ssm put-parameter --name /shopflow/aws/control/lease-expires-at",
        r"ssm put-parameter --name /shopflow/aws/control/session",
        r"ssm put-parameter --name /shopflow/aws/kafka/cdc-epoch .*--value 1 ",
        r"tofu .*cluster apply -auto-approve",
        r"helm upgrade --install argocd argo-cd .*--version 10.9.6",
        r"ssm put-parameter --name /shopflow/aws/control/pg-initialized",
        r"kubectl .*apply -f .*shop-db-\S+-initial",
        r"ssm put-parameter --name /shopflow/aws/control/pg-backup-pointer",
        r"curl .*-X POST",
        r"scheduler create-schedule",
        r"gh workflow enable cloud-reaper.yml",
    ]
    positions = [fake.index_of(s) for s in steps]
    assert positions == sorted(positions), list(zip(steps, positions, strict=True))

    marker = next(c for c in fake.calls("aws") if "/shopflow/aws/control/pg-initialized" in c.argv)
    assert "--overwrite" not in marker.argv, "the marker is write-once"

    hook_calls = record.read_text()
    kubeconfig = str(fake.home / ".kube" / "shopflow-aws")
    assert hook_calls.index("--check") < hook_calls.index(f"platform-root-apps.sh KUBECONFIG={kubeconfig} --overlay aws --revision main")
    assert "pg.recoveryFrom= " in hook_calls and "aws.vpcId=vpc-0123456789abcdef0" in hook_calls
    assert f"cdc-epoch.sh KUBECONFIG={kubeconfig} wait --epoch 1" in hook_calls

    smoke = next(c for c in fake.calls("kubectl") if "deploy/trino-coordinator" in c.argv)
    assert smoke.stdin.strip() == TRINO_PASSWORD, "the Trino password reaches the CLI through stdin"
    assert all(TRINO_PASSWORD not in arg for c in fake.calls() for arg in c.argv), "never on a command line"

    leases = [
        c.argv[c.argv.index("--value") + 1]
        for c in fake.calls("aws")
        if "/shopflow/aws/control/lease-expires-at" in c.argv and "put-parameter" in c.argv
    ]
    assert len(leases) == 2, "provisional lease before apply, final lease after the smoke test"
    final = datetime.fromisoformat(leases[-1])
    assert timedelta(hours=2, minutes=59) < final - datetime.now(UTC) <= timedelta(hours=3)
    schedule = next(c for c in fake.calls("aws") if "create-schedule" in c.argv)
    assert datetime.fromisoformat(schedule.argv[schedule.argv.index("--start-date") + 1]) == final + timedelta(hours=1)

    timings = json.loads(next((fake.tmp / "out").glob("*/timings.json")).read_text())
    assert {"rto_infra_seconds", "rto_service_seconds"} <= timings.keys() and timings["postgres"] == "initdb"
    assert "RTO-infra (apply -> node Ready)" in result.stderr


def test_missing_root_app_hook_fails_before_anything_bills(fake, hooks):
    env, _ = hooks
    full_session(fake)

    result = fake.run("cloud-up.sh", env={**env, "ROOT_APPS_HOOK": "/nonexistent/platform-root-apps.sh"})

    assert result.returncode != 0
    assert "missing /nonexistent/platform-root-apps.sh (sf-platform root app mechanism)" in result.stderr
    assert not any("apply" in c.argv for c in fake.calls("tofu")), "checked in preflight, before layer 2"


# ---- with the real sf-platform hook (scripts/platform-root-apps.sh) in a copy of the repo ----------------------


@pytest.fixture
def repo_copy(tmp_path):
    """The scripts, contract and Argo CD root-app files, plus minimal aws profiles (they land with sf-platform)."""
    root = tmp_path / "repo"
    for rel in (
        "scripts",
        "infra/cloud-contract.json",
        "deploy/argocd/root-app.yaml",
        "deploy/argocd/profiles/_common",
    ):
        src, dst = REPO_ROOT / rel, root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
    for profile in ("core", "obs-lite"):
        prof = root / "deploy/argocd/profiles-aws" / profile
        prof.mkdir(parents=True)
        (prof / "kustomization.yaml").write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n")
    chart = tmp_path / "argocd-chart.yaml"
    chart.write_text("repo: https://argoproj.github.io/argo-helm\nchart: argo-cd\nversion: 10.9.6\n")
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    env = {"ARGOCD_CHART_FILE": str(chart), "ARGOCD_VALUES": str(values), "ARGOCD_AWS_VALUES": str(values)}
    return root, env


def test_dry_run_end_to_end_with_the_real_root_app_hook(fake, repo_copy):
    root, env = repo_copy
    preflight(fake, pointer=json.dumps(POINTER), marker="20261001t080000z", epoch="7")

    result = fake.run_at(root, "cloud-up.sh", "--dry-run", "--profiles", "core,obs-lite", env=env)

    assert result.returncode == 0, result.stderr
    assert fake.mutations() == []
    assert "root apps: profiles core,obs-lite and session parameters accepted" in result.stderr
    printed = result.stderr[result.stderr.index("root Applications that would be applied") :]
    for expected in (
        "name: root-core",
        "name: root-obs-lite",
        "path: deploy/argocd/profiles-aws/core",
        "shopflow.io/overlay: aws",
        "/data/aws.accountId",
        "/data/aws.vpcId",
        "vpc-0123456789abcdef0",
        "shop-db-20261001t080000z",
    ):
        assert expected in printed, expected


def test_unknown_aws_profile_stops_in_preflight(fake, repo_copy):
    root, env = repo_copy
    preflight(fake)

    result = fake.run_at(root, "cloud-up.sh", "--profiles", "core,nope", env=env)

    assert result.returncode != 0
    assert "unknown profile for overlay aws: nope" in result.stderr
    assert not any("apply" in c.argv for c in fake.calls("tofu"))
