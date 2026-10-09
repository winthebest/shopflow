"""cloud-down: graceful order, resumability, dry-run safety. All CLIs are fakes."""

import json

from script_harness import Harness, operator

SESSION = {"id": "20261009t120000z", "operatorCidr": "203.0.113.10/32"}
LB_ARN = "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:loadbalancer/net/k8s-shop/abc"
FINAL = "shop-db-20261009t120000z-final"
APPS = {
    "items": [
        {"metadata": {"name": "shop"}, "spec": {"sources": [{"path": "deploy/platform/shop/aws"}], "syncPolicy": {"automated": {}}}},
        {"metadata": {"name": "root-core"}, "spec": {"source": {"path": "deploy/argocd/profiles/core"}, "syncPolicy": {"automated": {}}}},
        {"metadata": {"name": "manual"}, "spec": {"source": {"path": "deploy/platform/x"}, "syncPolicy": {}}},
    ]
}
EMPTY_REGION = [
    (r"ec2 describe-instances", {"Reservations": []}),
    (r"ec2 describe-volumes --filters Name=status", {"Volumes": []}),
    (r"ec2 describe-network-interfaces", {"NetworkInterfaces": []}),
    (r"ec2 describe-addresses", {"Addresses": []}),
    (r"ec2 describe-nat-gateways", {"NatGateways": []}),
]


def session_world(h: Harness, *, cluster: str = "ACTIVE", desired: str = "2") -> Harness:
    operator(h)
    h.on("aws", r"ssm get-parameter --name /shopflow/aws/control/session ", json.dumps(SESSION))
    h.on("aws", r"ssm get-parameter --name /shopflow/aws/control/lease-expires-at ", "2026-10-09T16:00:00Z")
    h.not_found("aws", r"ssm get-parameter", "ParameterNotFound", "GetParameter")
    if cluster == "ABSENT":
        h.not_found("aws", r"eks describe-cluster", "ResourceNotFoundException", "DescribeCluster")
    else:
        h.on("aws", r"eks describe-cluster", f"{cluster}\n")
    h.on("aws", r"eks list-nodegroups", "shopflow-spot-1\n")
    h.on("aws", r"eks describe-nodegroup .*desiredSize", f"{desired}\n")
    # Argo CD
    h.on("kubectl", r"-n argocd get applications.argoproj.io -o json", json_out=APPS)
    h.on("kubectl", r"-n argocd get applications.argoproj.io -o name", "application.argoproj.io/root-core\napplication.argoproj.io/shop\n")
    # Postgres final backup
    h.on("kubectl", r"shop-db -o jsonpath=\{.status.currentPrimary\}", "shop-db-1")
    h.on("kubectl", r"exec shop-db-1 .*pg_switch_wal", "000000010000000000000007\n")
    h.on("kubectl", r"exec shop-db-1 .*pg_stat_archiver", "000000010000000000000007\n")
    h.on("kubectl", rf"backups.postgresql.cnpg.io {FINAL} -o jsonpath", ["", "", "completed"])
    h.on(
        "kubectl",
        rf"backups.postgresql.cnpg.io {FINAL} -o json",
        json_out={"status": {"serverName": "shop-db-20261009t120000z", "backupId": "20261009T170000"}},
    )
    # Evidence
    h.on("kubectl", r"get pods -l app.kubernetes.io/name=prometheus", "prometheus-0")
    h.on("kubectl", r"api/v1/admin/tsdb/snapshot", json_out={"status": "success", "data": {"name": "20261009T1700Z-abc"}})
    # Load balancers: present on the first look, gone afterwards
    h.on(
        "kubectl",
        r"get services --all-namespaces -o json",
        json_out={"items": [{"metadata": {"namespace": "envoy-gateway-system", "name": "envoy-shop"}, "spec": {"type": "LoadBalancer"}}]},
    )
    h.on("aws", r"elbv2 describe-load-balancers", [f"{LB_ARN}\n", ""])
    h.on(
        "aws",
        r"elbv2 describe-tags",
        json_out={"TagDescriptions": [{"ResourceArn": LB_ARN, "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "shopflow"}]}]},
    )
    # Stateful data
    h.on("kubectl", r"get pv -o json", json_out={"items": []})
    h.on("aws", r"ec2 describe-volumes --filters Name=tag:project", "")
    # Orphan check
    h.on("aws", r"ec2 describe-regions", "ap-southeast-1\n")
    for match, out in EMPTY_REGION:
        h.on("aws", match, json_out=out)
    return h


def test_graceful_teardown_runs_in_order(fake):
    session_world(fake)

    result = fake.run("cloud-down.sh")

    assert result.returncode == 0, result.stderr
    steps = [
        r"patch applications.argoproj.io root-core",
        r"patch applications.argoproj.io shop ",
        r"exec shop-db-1 .*CHECKPOINT",
        r"kubectl .*apply -f .*backup-" + FINAL,
        r"ssm put-parameter --name /shopflow/aws/control/pg-backup-pointer",
        r"s3 cp --recursive .*s3://shopflow-data-123456789012/evidence/20261009t120000z/",
        r"delete gateways.gateway.networking.k8s.io --all",
        r"delete service envoy-shop",
        r"delete kafkas.kafka.strimzi.io --all",
        r"delete persistentvolumeclaims --all",
        r"helm uninstall argocd",
        r"tofu .*cluster destroy -auto-approve",
        r"ec2 describe-regions",
        r"ssm delete-parameter --name /shopflow/aws/control/lease-expires-at",
        r"scheduler delete-schedule",
    ]
    positions = [fake.index_of(s) for s in steps]
    assert positions == sorted(positions), list(zip(steps, positions, strict=True))
    assert not any("patch applications.argoproj.io manual" in c.line for c in fake.calls())


def test_final_backup_moves_the_pointer(fake):
    session_world(fake)

    assert fake.run("cloud-down.sh").returncode == 0
    put = next(c for c in fake.calls("aws") if "/shopflow/aws/control/pg-backup-pointer" in c.argv)
    value = json.loads(put.argv[put.argv.index("--value") + 1])
    assert value == {"serverName": "shop-db-20261009t120000z", "backupId": "20261009T170000"}


def test_dry_run_changes_nothing(fake):
    session_world(fake)

    result = fake.run("cloud-down.sh", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert fake.mutations() == []
    assert any(c.tool == "tofu" and "plan" in c.argv and "-destroy" in c.argv for c in fake.calls())
    assert "DRY-RUN: kube -n argocd patch" in result.stderr and "DRY-RUN: helm uninstall" in result.stderr
    assert "dry-run complete" in result.stderr


def test_rerun_without_cluster_skips_kubernetes(fake):
    session_world(fake, cluster="ABSENT")

    result = fake.run("cloud-down.sh")

    assert result.returncode == 0, result.stderr
    assert fake.calls("kubectl") == [] and fake.calls("helm") == []
    fake.index_of(r"tofu .*cluster destroy")
    fake.index_of(r"ssm delete-parameter --name /shopflow/aws/control/lease-expires-at")


def test_paused_cluster_needs_resume_or_force_api(fake):
    session_world(fake, desired="0")

    result = fake.run("cloud-down.sh")

    assert result.returncode != 0
    assert "cloud-resume" in result.stderr
    assert fake.mutations() == []


def test_force_api_uses_the_reaper_then_destroys(fake):
    session_world(fake, desired="0")

    result = fake.run("cloud-down.sh", "--force-api")

    assert result.returncode == 0, result.stderr
    assert fake.calls("kubectl") == []
    teardown = fake.index_of(r"uv run .*python -m reaper .*teardown --cluster shopflow --project shopflow --keep-cluster --wait")
    assert teardown < fake.index_of(r"tofu .*cluster destroy")


def test_orphans_fail_the_run_after_cleanup(fake):
    session_world(fake, cluster="ABSENT")
    fake.rules.insert(
        0,
        {
            "tool": "aws",
            "match": r"ec2 describe-volumes --filters Name=status",
            "stdout": json.dumps({"Volumes": [{"VolumeId": "vol-0stranger", "Tags": []}]}),
            "exit": 0,
        },
    )

    result = fake.run("cloud-down.sh")

    assert result.returncode != 0
    assert "UNKNOWN  ap-southeast-1 volume vol-0stranger" in result.stderr
    fake.index_of(r"ssm delete-parameter --name /shopflow/aws/control/lease-expires-at")
    assert not any("delete-volume" in c.line for c in fake.calls())
