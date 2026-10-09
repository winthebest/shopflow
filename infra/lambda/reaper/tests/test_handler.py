import json
from datetime import UTC, datetime

import pytest

from aws_world import CLUSTER, LEASE_PARAM, PROJECT
from reaper import __main__ as cli
from reaper.handler import ReaperError, Settings, lambda_handler, reap

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SETTINGS = Settings(
    cluster_name=CLUSTER,
    project=PROJECT,
    lease_parameter=LEASE_PARAM,
    schedule_group="shopflow",
    schedule_name="shopflow-reaper",
    alert_topic_arn="",
)


@pytest.fixture
def wired(world):
    """Alert topic with a queue subscribed, plus the per-session schedule cloud-up creates."""
    sns, sqs = world.client("sns"), world.client("sqs")
    topic = sns.create_topic(Name="shopflow-alerts")["TopicArn"]
    queue_url = sqs.create_queue(QueueName="alerts")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)
    scheduler = world.client("scheduler")
    scheduler.create_schedule_group(Name="shopflow")
    scheduler.create_schedule(
        GroupName="shopflow",
        Name="shopflow-reaper",
        ScheduleExpression="rate(15 minutes)",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": "arn:aws:lambda:ap-southeast-1:123456789012:function:shopflow-reaper",
            "RoleArn": "arn:aws:iam::123456789012:role/x",
        },
    )
    settings = Settings(**{**SETTINGS.__dict__, "alert_topic_arn": topic})

    def messages() -> list[str]:
        out = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
        return [json.loads(m["Body"])["Subject"] for m in out]

    def schedule_exists() -> bool:
        return any(s["Name"] == "shopflow-reaper" for s in scheduler.list_schedules(GroupName="shopflow")["Schedules"])

    return world, settings, messages, schedule_exists


def set_lease(world, value: str) -> None:
    world.client("ssm").put_parameter(Name=LEASE_PARAM, Value=value, Type="String", Overwrite=True)


def test_valid_lease_skips_everything(wired):
    world, settings, messages, schedule_exists = wired
    world.create_cluster()
    set_lease(world, "2026-10-09T13:00:00Z")

    result = reap(settings, world.session, now=NOW, time_left=lambda: 600, sleep=lambda _: None)

    assert result["status"] == "skipped"
    assert world.cluster_exists() and schedule_exists() and messages() == []


def test_expired_lease_tears_down_and_removes_its_schedule(wired):
    world, settings, messages, schedule_exists = wired
    world.create_cluster()
    world.create_load_balancer("k8s-shop-gateway", cluster=CLUSTER)
    set_lease(world, "2026-10-09T10:00:00Z")

    result = reap(settings, world.session, now=NOW, time_left=lambda: 600, sleep=lambda _: None)

    assert result["status"] == "done"
    assert not world.cluster_exists() and world.load_balancer_arns() == set()
    assert not schedule_exists()
    assert messages() == ["[shopflow] reaper tore down shopflow"]


def test_missing_lease_means_expired(wired):
    world, settings, _, _ = wired
    world.create_cluster()

    assert reap(settings, world.session, now=NOW, time_left=lambda: 600, sleep=lambda _: None)["status"] == "done"
    assert not world.cluster_exists()


def test_nothing_left_removes_schedule_without_alert(wired):
    world, settings, messages, schedule_exists = wired

    result = reap(settings, world.session, now=NOW, time_left=lambda: 600, sleep=lambda _: None)

    assert result["status"] == "done" and result["deleted"] == []
    assert not schedule_exists() and messages() == []


def test_errors_alert_and_fail_the_invocation(wired, monkeypatch):
    world, settings, messages, schedule_exists = wired
    set_lease(world, "2026-10-09T10:00:00Z")

    from reaper import handler

    class DeniedTeardown(handler.Teardown):
        def run(self, **_):
            progress = handler.Teardown.run_pass(self, delete_cluster=False)
            progress.errors.append("load-balancer:x: AccessDenied")
            return progress

    monkeypatch.setattr(handler, "Teardown", DeniedTeardown)

    with pytest.raises(ReaperError):
        reap(settings, world.session, now=NOW, time_left=lambda: 600, sleep=lambda _: None)
    assert messages() == ["[shopflow] reaper FAILED for shopflow"]
    assert schedule_exists()


def test_lambda_handler_reads_settings_from_environment(wired, monkeypatch):
    world, settings, _, _ = wired
    set_lease(world, "2999-01-01T00:00:00Z")
    for key, value in {
        "REAPER_CLUSTER_NAME": CLUSTER,
        "REAPER_PROJECT": PROJECT,
        "REAPER_LEASE_PARAMETER": LEASE_PARAM,
        "REAPER_SCHEDULE_GROUP": "shopflow",
        "REAPER_SCHEDULE_NAME": "shopflow-reaper",
        "REAPER_ALERT_TOPIC_ARN": settings.alert_topic_arn,
    }.items():
        monkeypatch.setenv(key, value)

    class Context:
        @staticmethod
        def get_remaining_time_in_millis() -> int:
            return 900_000

    assert lambda_handler({}, Context())["status"] == "skipped"


def test_cli_lease_exit_codes(world, capsys):
    assert cli.main(["lease", "--parameter", LEASE_PARAM], session=world.session) == 0
    set_lease(world, "2999-01-01T00:00:00Z")
    assert cli.main(["lease", "--parameter", LEASE_PARAM], session=world.session) == cli.EXIT_LEASE_VALID
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["expired"] is False


def test_cli_keep_cluster_and_dry_run(world):
    world.create_cluster()

    assert cli.main(["teardown", "--cluster", CLUSTER, "--project", PROJECT, "--dry-run"], session=world.session) == 0
    assert world.client("eks").list_nodegroups(clusterName=CLUSTER)["nodegroups"] == ["shopflow-spot-0"]

    code = cli.main(
        ["teardown", "--cluster", CLUSTER, "--project", PROJECT, "--keep-cluster", "--wait", "--interval", "0"],
        session=world.session,
    )
    assert code == 0
    assert world.cluster_exists()
    assert world.client("eks").list_nodegroups(clusterName=CLUSTER)["nodegroups"] == []
