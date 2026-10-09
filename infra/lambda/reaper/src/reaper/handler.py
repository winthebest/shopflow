"""Lambda entry point: the backup kill switch that does not depend on GitHub.

EventBridge Scheduler invokes it every few minutes from lease + grace. It re-reads the lease (the
operator may have extended it without moving the schedule), tears the session down, and removes
its own schedule once nothing is left. Any failure raises, which trips the CloudWatch alarm.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3

from reaper.lease import read_lease
from reaper.teardown import Teardown

log = logging.getLogger()
log.setLevel(logging.INFO)

# Stop starting new passes this many seconds before the Lambda timeout.
SAFETY_MARGIN_SECONDS = 90


class ReaperError(RuntimeError):
    """Raised so the invocation counts as failed and the error alarm fires."""


@dataclass(frozen=True)
class Settings:
    cluster_name: str
    project: str
    lease_parameter: str
    schedule_group: str
    schedule_name: str
    alert_topic_arn: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        return cls(
            cluster_name=env["REAPER_CLUSTER_NAME"],
            project=env["REAPER_PROJECT"],
            lease_parameter=env["REAPER_LEASE_PARAMETER"],
            schedule_group=env["REAPER_SCHEDULE_GROUP"],
            schedule_name=env["REAPER_SCHEDULE_NAME"],
            alert_topic_arn=env["REAPER_ALERT_TOPIC_ARN"],
        )


def _notify(sns, settings: Settings, subject: str, body: dict) -> None:
    sns.publish(TopicArn=settings.alert_topic_arn, Subject=subject[:100], Message=json.dumps(body, indent=2))


def _delete_schedule(scheduler, settings: Settings) -> None:
    try:
        scheduler.delete_schedule(GroupName=settings.schedule_group, Name=settings.schedule_name)
    except scheduler.exceptions.ResourceNotFoundException:
        pass


def reap(settings: Settings, session, *, now: datetime, time_left, interval: float = 30, sleep=time.sleep) -> dict:
    lease = read_lease(session.client("ssm"), settings.lease_parameter, now)
    if not lease.expired:
        log.info("lease valid until %s; nothing to do", lease.as_dict()["expiresAt"])
        return {"status": "skipped", "lease": lease.as_dict()}

    teardown = Teardown(
        cluster_name=settings.cluster_name,
        project=settings.project,
        eks=session.client("eks"),
        elbv2=session.client("elbv2"),
        ec2=session.client("ec2"),
    )
    progress = teardown.run(delete_cluster=True, time_left=time_left, interval=interval, sleep=sleep)
    result = {"status": "done" if progress.done else "in-progress", "lease": lease.as_dict(), **progress.as_dict()}
    sns = session.client("sns")

    if progress.errors:
        _notify(sns, settings, f"[shopflow] reaper FAILED for {settings.cluster_name}", result)
        raise ReaperError("; ".join(progress.errors))
    if progress.done:
        _delete_schedule(session.client("scheduler"), settings)
        if progress.deleted:
            _notify(sns, settings, f"[shopflow] reaper tore down {settings.cluster_name}", result)
    log.info(json.dumps(result))
    return result


def lambda_handler(event, context) -> dict:
    settings = Settings.from_env(os.environ)
    return reap(
        settings,
        boto3.session.Session(),
        now=datetime.now(UTC),
        time_left=lambda: context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_SECONDS,
    )
