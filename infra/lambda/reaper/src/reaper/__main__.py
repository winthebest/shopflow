"""CLI for the same teardown logic, used by the GitHub reaper workflow and ``cloud-down --force-api``.

python -m reaper lease --parameter /shopflow/aws/control/lease-expires-at
    exit 0 when the lease is expired (or missing), 10 when it is still valid
python -m reaper teardown --cluster shopflow --project shopflow [--keep-cluster] [--wait]
    exit 0 when nothing is left, 3 when resources are still being deleted, 1 on errors
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime

import boto3

from reaper.lease import read_lease
from reaper.teardown import Teardown

EXIT_LEASE_VALID = 10
EXIT_NOT_DONE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m reaper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", help="AWS region (defaults to the AWS SDK configuration)")
    sub = parser.add_subparsers(dest="command", required=True)

    lease = sub.add_parser("lease", help="report whether the session lease has expired")
    lease.add_argument("--parameter", required=True, help="SSM parameter holding the lease")

    teardown = sub.add_parser("teardown", help="delete the session's billable resources")
    teardown.add_argument("--cluster", required=True)
    teardown.add_argument("--project", required=True)
    teardown.add_argument("--keep-cluster", action="store_true", help="stop before deleting the cluster (OpenTofu destroys it next)")
    teardown.add_argument("--wait", action="store_true", help="repeat passes until nothing is left or --timeout")
    teardown.add_argument("--timeout", type=float, default=1800, help="seconds to keep waiting (default 1800)")
    teardown.add_argument("--interval", type=float, default=30, help="seconds between passes (default 30)")
    teardown.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    return parser


def main(argv: list[str] | None = None, session=None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    session = session or boto3.session.Session(region_name=args.region)

    if args.command == "lease":
        status = read_lease(session.client("ssm"), args.parameter, datetime.now(UTC))
        print(json.dumps(status.as_dict()))
        return 0 if status.expired else EXIT_LEASE_VALID

    teardown = Teardown(
        cluster_name=args.cluster,
        project=args.project,
        eks=session.client("eks"),
        elbv2=session.client("elbv2"),
        ec2=session.client("ec2"),
        dry_run=args.dry_run,
    )
    deadline = time.monotonic() + (args.timeout if args.wait else 0)
    progress = teardown.run(
        delete_cluster=not args.keep_cluster,
        time_left=lambda: deadline - time.monotonic(),
        interval=args.interval,
    )
    print(json.dumps(progress.as_dict(), indent=2))
    if progress.errors:
        return 1
    return 0 if progress.done or progress.dry_run else EXIT_NOT_DONE


if __name__ == "__main__":
    sys.exit(main())
