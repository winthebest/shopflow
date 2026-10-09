"""Delete one EKS session's billable resources through AWS APIs only (no kubectl, no OpenTofu).

One pass walks: load balancers -> target groups -> node groups -> available volumes and network
interfaces -> cluster (optional) -> load balancer security groups. Every step is idempotent and
callers repeat passes until ``Progress.done``. A pass that deleted or still sees something reports
``waiting``, so "done" always means a later pass found nothing left. That matters because the AWS
Load Balancer Controller can re-create a deleted NLB until the node groups (and the controller
pods with them) are gone.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

ELBV2_CLUSTER_TAG = "elbv2.k8s.aws/cluster"
CSI_VOLUME_TAG_KEYS = ("ebs.csi.aws.com/cluster", "CSIVolumeName")
NOT_FOUND_CODES = {
    "LoadBalancerNotFound",
    "TargetGroupNotFound",
    "ResourceNotFoundException",
    "InvalidVolume.NotFound",
    "InvalidNetworkInterfaceID.NotFound",
    "InvalidGroup.NotFound",
}
BUSY_CODES = {"ResourceInUse", "DependencyViolation", "VolumeInUse", "InvalidNetworkInterface.InUse", "ResourceInUseException"}
DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
ELBV2_TAGS_BATCH = 20


@dataclass
class Progress:
    dry_run: bool = False
    deleted: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return not self.waiting and not self.errors

    def merge(self, later: Progress) -> Progress:
        """Combine passes: keep every deletion, but the latest view of what is still pending."""
        return Progress(
            dry_run=later.dry_run,
            deleted=self.deleted + later.deleted,
            waiting=later.waiting,
            errors=later.errors,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "done": self.done,
            "dryRun": self.dry_run,
            "deleted": self.deleted,
            "waiting": self.waiting,
            "errors": self.errors,
        }


def _tags_match(tags: Iterable[dict], key: str, value: str) -> bool:
    return any(t.get("Key") == key and t.get("Value") == value for t in tags)


class Teardown:
    def __init__(self, *, cluster_name: str, project: str, eks, elbv2, ec2, dry_run: bool = False) -> None:
        self.cluster_name = cluster_name
        self.project = project
        self.eks = eks
        self.elbv2 = elbv2
        self.ec2 = ec2
        self.dry_run = dry_run

    def run(
        self,
        *,
        delete_cluster: bool,
        time_left: Callable[[], float],
        interval: float = 30,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Progress:
        """Repeat passes until done, out of time, or after a single dry-run pass."""
        total = Progress(dry_run=self.dry_run)
        while True:
            total = total.merge(self.run_pass(delete_cluster=delete_cluster))
            if total.done or total.errors or self.dry_run or time_left() < interval:
                return total
            log.info("waiting %ss: %s", interval, "; ".join(total.waiting))
            sleep(interval)

    def run_pass(self, *, delete_cluster: bool) -> Progress:
        progress = Progress(dry_run=self.dry_run)
        self._load_balancers(progress)
        self._target_groups(progress)
        cluster_status = self._cluster_status()
        nodegroups_left = self._nodegroups(progress) if cluster_status else 0
        self._volumes(progress)
        self._network_interfaces(progress)
        cluster_gone = self._cluster(progress, cluster_status, delete_cluster=delete_cluster, nodegroups_left=nodegroups_left)
        self._security_groups(progress, required=delete_cluster and cluster_gone)
        return progress

    # -- helpers -----------------------------------------------------------------------------

    def _delete(self, progress: Progress, kind: str, ident: str, call: Callable[[], object]) -> str:
        """Run one delete call. Returns "deleted", "gone", "busy" or "denied"."""
        label = f"{kind}:{ident}"
        if self.dry_run:
            log.info("dry-run: would delete %s", label)
            progress.deleted.append(label)
            return "deleted"
        try:
            call()
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in NOT_FOUND_CODES:
                return "gone"
            if code in BUSY_CODES:
                log.info("%s is still in use (%s)", label, code)
                return "busy"
            if code in DENIED_CODES:
                progress.errors.append(f"{label}: {code} (is it tagged project={self.project}?)")
                return "denied"
            raise
        log.info("deleted %s", label)
        progress.deleted.append(label)
        return "deleted"

    def _elbv2_owned(self, arns: list[str]) -> list[str]:
        owned: list[str] = []
        for start in range(0, len(arns), ELBV2_TAGS_BATCH):
            batch = arns[start : start + ELBV2_TAGS_BATCH]
            for desc in self.elbv2.describe_tags(ResourceArns=batch)["TagDescriptions"]:
                if _tags_match(desc.get("Tags", []), ELBV2_CLUSTER_TAG, self.cluster_name):
                    owned.append(desc["ResourceArn"])
        return owned

    # -- steps -------------------------------------------------------------------------------

    def _load_balancers(self, progress: Progress) -> None:
        pages = self.elbv2.get_paginator("describe_load_balancers").paginate()
        arns = [lb["LoadBalancerArn"] for page in pages for lb in page["LoadBalancers"]]
        owned = self._elbv2_owned(arns)
        for arn in owned:
            self._delete(progress, "load-balancer", arn, lambda arn=arn: self.elbv2.delete_load_balancer(LoadBalancerArn=arn))
        if owned:
            progress.waiting.append(f"{len(owned)} load balancer(s) of {self.cluster_name} were present")

    def _target_groups(self, progress: Progress) -> None:
        pages = self.elbv2.get_paginator("describe_target_groups").paginate()
        arns = [tg["TargetGroupArn"] for page in pages for tg in page["TargetGroups"]]
        owned = self._elbv2_owned(arns)
        for arn in owned:
            self._delete(progress, "target-group", arn, lambda arn=arn: self.elbv2.delete_target_group(TargetGroupArn=arn))
        if owned:
            progress.waiting.append(f"{len(owned)} target group(s) were present")

    def _cluster_status(self) -> str | None:
        """EKS status of the cluster, or None when it does not exist."""
        try:
            return self.eks.describe_cluster(name=self.cluster_name)["cluster"]["status"]
        except self.eks.exceptions.ResourceNotFoundException:
            return None

    def _nodegroups(self, progress: Progress) -> int:
        try:
            pages = self.eks.get_paginator("list_nodegroups").paginate(clusterName=self.cluster_name)
            names = [name for page in pages for name in page["nodegroups"]]
        except self.eks.exceptions.ResourceNotFoundException:
            return 0
        for name in names:
            status = self.eks.describe_nodegroup(clusterName=self.cluster_name, nodegroupName=name)["nodegroup"]["status"]
            if status != "DELETING":
                self._delete(
                    progress,
                    "nodegroup",
                    name,
                    lambda name=name: self.eks.delete_nodegroup(clusterName=self.cluster_name, nodegroupName=name),
                )
        if names:
            progress.waiting.append(f"{len(names)} node group(s) deleting")
        return len(names)

    def _volumes(self, progress: Progress) -> None:
        pages = self.ec2.get_paginator("describe_volumes").paginate(
            Filters=[
                {"Name": "tag:project", "Values": [self.project]},
                {"Name": "status", "Values": ["available", "in-use", "creating"]},
            ]
        )
        attached_csi = 0
        for page in pages:
            for vol in page["Volumes"]:
                if vol["State"] == "available":
                    self._delete(progress, "volume", vol["VolumeId"], lambda v=vol["VolumeId"]: self.ec2.delete_volume(VolumeId=v))
                elif any(t["Key"] in CSI_VOLUME_TAG_KEYS for t in vol.get("Tags", [])):
                    # A persistent volume still attached to a terminating node; it becomes
                    # available (and deletable) once the node is gone.
                    attached_csi += 1
        if attached_csi:
            progress.waiting.append(f"{attached_csi} persistent volume(s) still attached")

    def _network_interfaces(self, progress: Progress) -> None:
        pages = self.ec2.get_paginator("describe_network_interfaces").paginate(
            Filters=[
                {"Name": "tag:project", "Values": [self.project]},
                {"Name": "status", "Values": ["available"]},
            ]
        )
        for page in pages:
            for eni in page["NetworkInterfaces"]:
                eni_id = eni["NetworkInterfaceId"]
                self._delete(
                    progress, "network-interface", eni_id, lambda e=eni_id: self.ec2.delete_network_interface(NetworkInterfaceId=e)
                )

    def _cluster(self, progress: Progress, status: str | None, *, delete_cluster: bool, nodegroups_left: int) -> bool:
        """Returns True when the cluster no longer exists."""
        if status is None:
            return True
        if not delete_cluster:
            return False
        if nodegroups_left:
            progress.waiting.append("cluster waits for its node groups")
            return False
        if status != "DELETING":
            result = self._delete(progress, "cluster", self.cluster_name, lambda: self.eks.delete_cluster(name=self.cluster_name))
            if result == "gone":
                return True
        progress.waiting.append("cluster deleting")
        return False

    def _security_groups(self, progress: Progress, *, required: bool) -> None:
        """Security groups the LB controller created for NLBs. They block nothing billable, so they
        only hold up "done" once the cluster (and its security group rules) are gone."""
        pages = self.ec2.get_paginator("describe_security_groups").paginate(
            Filters=[
                {"Name": f"tag:{ELBV2_CLUSTER_TAG}", "Values": [self.cluster_name]},
                {"Name": "tag:project", "Values": [self.project]},
            ]
        )
        blocked = 0
        for page in pages:
            for sg in page["SecurityGroups"]:
                sg_id = sg["GroupId"]
                result = self._delete(progress, "security-group", sg_id, lambda s=sg_id: self.ec2.delete_security_group(GroupId=s))
                blocked += result == "busy"
        if blocked and required:
            progress.waiting.append(f"{blocked} load balancer security group(s) still referenced")
