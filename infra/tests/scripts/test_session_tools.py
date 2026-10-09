"""cloud-extend, cloud-pause, aws-seed-params, aws-orphan-check, cloud-reap. All CLIs are fakes."""

import json
from datetime import UTC, datetime, timedelta

from script_harness import REAPER_ARN, Harness, operator


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def value_of(call, flag: str) -> str:
    return call.argv[call.argv.index(flag) + 1]


# ---- cloud-extend -------------------------------------------------------------------------------


def extend_world(h: Harness, lease: str | None) -> Harness:
    operator(h)
    h.on("aws", r"eks describe-cluster --name", "ACTIVE\n")
    if lease:
        h.on("aws", r"ssm get-parameter --name /shopflow/aws/control/lease-expires-at ", lease)
    h.not_found("aws", r"ssm get-parameter", "ParameterNotFound", "GetParameter")
    return h


def test_extend_adds_hours_and_moves_the_backup_reaper(fake):
    lease = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    extend_world(fake, iso(lease))

    result = fake.run("cloud-extend.sh", "--hours", "2")

    assert result.returncode == 0, result.stderr
    put = next(c for c in fake.calls("aws") if "put-parameter" in c.argv)
    new = datetime.fromisoformat(value_of(put, "--value"))
    assert new == lease + timedelta(hours=2)
    update = next(c for c in fake.calls("aws") if "update-schedule" in c.argv)
    assert datetime.fromisoformat(value_of(update, "--start-date")) == new + timedelta(hours=1)
    assert not any(c.tool in ("kubectl", "tofu") for c in fake.calls()), "extend only touches SSM and the schedule"


def test_extend_is_capped(fake):
    extend_world(fake, iso(datetime.now(UTC) + timedelta(hours=1)))

    result = fake.run("cloud-extend.sh", "--hours", "20")

    assert result.returncode == 0, result.stderr
    put = next(c for c in fake.calls("aws") if "put-parameter" in c.argv)
    assert datetime.fromisoformat(value_of(put, "--value")) - datetime.now(UTC) <= timedelta(hours=8)
    assert "capping the lease" in result.stderr


def test_extend_after_expiry_counts_from_now(fake):
    extend_world(fake, iso(datetime.now(UTC) - timedelta(hours=3)))

    assert fake.run("cloud-extend.sh", "--hours", "1").returncode == 0
    put = next(c for c in fake.calls("aws") if "put-parameter" in c.argv)
    assert abs(datetime.fromisoformat(value_of(put, "--value")) - (datetime.now(UTC) + timedelta(hours=1))) < timedelta(minutes=1)


# ---- cloud-pause ----------------------------------------------------------------------------------


def pause_world(h: Harness) -> Harness:
    operator(h)
    h.on("aws", r"eks describe-cluster --name", "ACTIVE\n")
    h.on("aws", r"eks list-nodegroups", "shopflow-spot-1\n")
    h.on("aws", r"describe-nodegroup .*desiredSize", "2\n")
    h.on("aws", r"describe-nodegroup .*maxSize", "3\n")
    h.on("aws", r"ssm get-parameter --name /shopflow/aws/control/resume-desired ", '{"shopflow-spot-1":2}')
    h.not_found("aws", r"ssm get-parameter", "ParameterNotFound", "GetParameter")
    h.on("kubectl", r"get nodes -o json", json_out={"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]})
    return h


def test_pause_records_size_then_scales_to_zero(fake):
    pause_world(fake)

    result = fake.run("cloud-pause.sh")

    assert result.returncode == 0, result.stderr
    assert fake.index_of(r"put-parameter --name /shopflow/aws/control/resume-desired .*shopflow-spot-1") < fake.index_of(
        r"update-nodegroup-config .*desiredSize=0"
    )
    assert "do not keep it paused overnight" in result.stderr.lower()


def test_resume_restores_recorded_size(fake):
    pause_world(fake)

    result = fake.run("cloud-pause.sh", "--resume")

    assert result.returncode == 0, result.stderr
    fake.index_of(r"update-nodegroup-config .*minSize=0,maxSize=3,desiredSize=2")
    fake.index_of(r"delete-parameter --name /shopflow/aws/control/resume-desired")


# ---- aws-seed-params ------------------------------------------------------------------------------

SECRETS = {
    "shop": {"db-app-password": "S3CRET-shop"},
    "kafka": {"connect-scram": "S3CRET-kafka"},
    "observability": {"grafana-admin": {"admin-user": "admin", "admin-password": "S3CRET-grafana"}},
}


def seed_world(h: Harness) -> Harness:
    operator(h)
    h.on("aws", r"describe-parameters .*Values=/shopflow/aws/shop/db-app-password", "1\n")
    h.on("aws", r"describe-parameters", "0\n")
    return h


def test_seed_creates_missing_keeps_existing_and_never_exposes_values(fake):
    seed_world(fake)

    result = fake.run("aws-seed-params.sh", stdin=json.dumps(SECRETS))

    assert result.returncode == 0, result.stderr
    payloads = {p["Name"]: p for p in (json.loads(c.stdin) for c in fake.calls("aws") if "put-parameter" in c.argv)}
    assert set(payloads) == {"/shopflow/aws/kafka/connect-scram", "/shopflow/aws/observability/grafana-admin"}
    payload = payloads["/shopflow/aws/kafka/connect-scram"]
    assert payload["Type"] == "SecureString" and payload["KeyId"] == "alias/aws/ssm"
    assert payload["Value"] == "S3CRET-kafka" and "Overwrite" not in payload
    # A multi-key Secret is one parameter holding a JSON object; the ExternalSecret reads each key as a property.
    grafana = json.loads(payloads["/shopflow/aws/observability/grafana-admin"]["Value"])
    assert grafana == {"admin-user": "admin", "admin-password": "S3CRET-grafana"}
    assert all("S3CRET" not in arg for c in fake.calls() for arg in c.argv), "secrets must never be on a command line"
    assert "S3CRET" not in result.stderr + result.stdout


def test_seed_rotate_overwrites(fake):
    seed_world(fake)

    assert fake.run("aws-seed-params.sh", "--rotate", stdin=json.dumps(SECRETS)).returncode == 0
    payloads = [json.loads(c.stdin) for c in fake.calls("aws") if "put-parameter" in c.argv]
    shop = next(p for p in payloads if p["Name"] == "/shopflow/aws/shop/db-app-password")
    assert shop["Overwrite"] is True and "Tags" not in shop


def test_seed_rejects_unknown_namespace(fake):
    seed_world(fake)

    result = fake.run("aws-seed-params.sh", stdin=json.dumps({"control": {"lease": "x"}}))

    assert result.returncode != 0 and "not in eso_namespaces" in result.stderr
    assert fake.mutations() == []


# ---- aws-orphan-check -----------------------------------------------------------------------------


def orphan_world(h: Harness) -> Harness:
    operator(h)
    tagged = {"Volumes": [{"VolumeId": "vol-0ours", "Tags": [{"Key": "project", "Value": "shopflow"}]}]}
    stranger = {"Volumes": [{"VolumeId": "vol-0stranger", "Tags": [{"Key": "Name", "Value": "someone"}]}]}
    h.on("aws", r"--region ap-southeast-1 .*ec2 describe-volumes", json_out=tagged)
    h.on("aws", r"--region us-east-1 .*ec2 describe-volumes", json_out=stranger)
    h.on("aws", r"ec2 describe-instances", json_out={"Reservations": []})
    h.on("aws", r"ec2 describe-network-interfaces", json_out={"NetworkInterfaces": []})
    h.on("aws", r"ec2 describe-addresses", json_out={"Addresses": []})
    h.on("aws", r"ec2 describe-nat-gateways", json_out={"NatGateways": []})
    return h


def test_orphan_check_deletes_ours_and_reports_strangers(fake):
    orphan_world(fake)

    result = fake.run("aws-orphan-check.sh", "--regions", "ap-southeast-1 us-east-1", "--delete-tagged")

    assert result.returncode == 2, result.stderr
    deletes = [c.line for c in fake.calls("aws") if "delete-volume" in c.argv]
    assert deletes == ["aws --region ap-southeast-1 --output json ec2 delete-volume --volume-id vol-0ours"]
    assert "UNKNOWN  us-east-1 volume vol-0stranger" in result.stderr


def test_orphan_check_without_delete_reports_ours(fake):
    orphan_world(fake)

    result = fake.run("aws-orphan-check.sh", "--regions", "ap-southeast-1")

    assert result.returncode == 3
    assert "ORPHAN   ap-southeast-1 volume vol-0ours" in result.stderr
    assert fake.mutations() == []


# ---- cloud-reap -----------------------------------------------------------------------------------

REAP_ENV = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "42"}


def reap_world(h: Harness, *, lock_exit: int = 0, lock_age: timedelta = timedelta(minutes=5), lease_exit: int = 0) -> Harness:
    h.on("aws", r"sts get-caller-identity --query Account", "123456789012\n")
    h.on("aws", r"sts get-caller-identity --query Arn", f"{REAPER_ARN}\n")
    h.on("aws", r"eks describe-cluster --name", "ACTIVE\n")
    h.on("aws", r"elbv2 describe-load-balancers", "")
    h.on(
        "aws",
        r"s3api put-object .*cluster/terraform.tfstate.tflock",
        exit=lock_exit,
        stderr="An error occurred (PreconditionFailed)" if lock_exit else "",
    )
    h.on("aws", r"s3api head-object", iso(datetime.now(UTC) - lock_age).replace("Z", "+00:00") + "\n")
    h.on("aws", r"ssm get-parameter --name /shopflow/aws/control/lease-expires-at ", iso(datetime.now(UTC) - timedelta(hours=3)))
    h.not_found("aws", r"ssm get-parameter", "ParameterNotFound", "GetParameter")
    h.on("uv", r"python -m reaper .* lease ", exit=lease_exit)
    h.on("aws", r"ec2 describe-regions", "ap-southeast-1\n")
    for match, out in (
        (r"ec2 describe-instances", {"Reservations": []}),
        (r"ec2 describe-volumes", {"Volumes": []}),
        (r"ec2 describe-network-interfaces", {"NetworkInterfaces": []}),
        (r"ec2 describe-addresses", {"Addresses": []}),
        (r"ec2 describe-nat-gateways", {"NatGateways": []}),
    ):
        h.on("aws", match, json_out=out)
    return h


def test_reaper_reaps_an_expired_session_under_the_state_lock(fake):
    reap_world(fake)

    result = fake.run("cloud-reap.sh", env=REAP_ENV)

    assert result.returncode == 0, result.stderr
    steps = [
        r"s3api put-object .*--if-none-match \*",
        r"uv run .*python -m reaper .* lease --parameter /shopflow/aws/control/lease-expires-at",
        r"uv run .*teardown --cluster shopflow --project shopflow --keep-cluster --wait",
        r"tofu .*cluster destroy -auto-approve -input=false -lock=false",
        r"ec2 describe-regions",
        r"s3api delete-object .*cluster/terraform.tfstate.tflock",
    ]
    positions = [fake.index_of(s) for s in steps]
    assert positions == sorted(positions), list(zip(steps, positions, strict=True))


def test_reaper_leaves_a_valid_lease_alone(fake):
    reap_world(fake, lease_exit=10)

    result = fake.run("cloud-reap.sh", env=REAP_ENV)

    assert result.returncode == 0, result.stderr
    assert not any("teardown" in c.argv or "destroy" in c.argv for c in fake.calls())
    fake.index_of(r"s3api delete-object .*tflock")


def test_reaper_waits_for_a_running_apply(fake):
    reap_world(fake, lock_exit=254)
    fake.rules.insert(
        0,
        {
            "tool": "aws",
            "match": r"ssm get-parameter --name /shopflow/aws/control/lease-expires-at ",
            "stdout": "2999-01-01T00:00:00Z",
            "exit": 0,
        },
    )

    result = fake.run("cloud-reap.sh", env=REAP_ENV)

    assert result.returncode == 0, result.stderr
    assert "locked by another run" in result.stderr
    assert not any("delete-object" in c.argv or "destroy" in c.argv for c in fake.calls())


def test_reaper_alerts_on_a_stale_lock(fake):
    reap_world(fake, lock_exit=254, lock_age=timedelta(hours=5))

    result = fake.run("cloud-reap.sh", env=REAP_ENV)

    assert result.returncode != 0 and "state lock is" in result.stderr


def test_reaper_alerts_when_locked_and_long_overdue(fake):
    reap_world(fake, lock_exit=254)

    result = fake.run("cloud-reap.sh", env=REAP_ENV)

    assert result.returncode != 0 and "past its lease" in result.stderr


def test_reaper_has_nothing_to_do_without_a_cluster(fake):
    reap_world(fake)
    fake.rules.insert(
        0, {"tool": "aws", "match": r"eks describe-cluster --name", "stdout": "", "exit": 254, "stderr": "(ResourceNotFoundException)"}
    )

    result = fake.run("cloud-reap.sh", env=REAP_ENV)

    assert result.returncode == 0 and "nothing to reap" in result.stderr
    assert fake.mutations() == []
