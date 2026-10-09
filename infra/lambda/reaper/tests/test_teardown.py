from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from aws_world import CLUSTER, PROJECT
from reaper.teardown import Progress, Teardown


def make_teardown(world, **kwargs) -> Teardown:
    return Teardown(
        cluster_name=CLUSTER,
        project=PROJECT,
        eks=world.client("eks"),
        elbv2=world.client("elbv2"),
        ec2=world.client("ec2"),
        **kwargs,
    )


def passes(limit: int = 5):
    """time_left() that allows `limit` passes, so a test can never spin forever."""
    remaining = iter(range(limit, 0, -1))
    return lambda: next(remaining, 0)


def run_to_completion(teardown: Teardown, *, delete_cluster: bool) -> Progress:
    return teardown.run(delete_cluster=delete_cluster, time_left=passes(), interval=0.5, sleep=lambda _: None)


def test_deletes_only_this_clusters_load_balancers(world):
    ours = world.create_load_balancer("k8s-shop-gateway", cluster=CLUSTER)
    other = world.create_load_balancer("someone-else", cluster="another-cluster")
    untagged = world.create_load_balancer("untagged", cluster=None)

    run_to_completion(make_teardown(world), delete_cluster=False)

    assert world.load_balancer_arns() == {other, untagged}
    assert ours not in world.load_balancer_arns()


def test_deletes_target_groups_of_the_cluster(world):
    world.create_target_group("k8s-shop-tg", cluster=CLUSTER)
    kept = world.create_target_group("other-tg", cluster=None)

    run_to_completion(make_teardown(world), delete_cluster=False)

    remaining = {tg["TargetGroupArn"] for tg in world.client("elbv2").describe_target_groups()["TargetGroups"]}
    assert remaining == {kept}


def test_full_teardown_removes_nodegroups_then_cluster(world):
    world.create_cluster(nodegroups=2)

    progress = run_to_completion(make_teardown(world), delete_cluster=True)

    assert progress.done
    assert not world.cluster_exists()
    assert [d for d in progress.deleted if d.startswith("nodegroup:")] == ["nodegroup:shopflow-spot-0", "nodegroup:shopflow-spot-1"]
    assert progress.deleted[-1] == "cluster:shopflow"


def test_cluster_is_not_deleted_while_nodegroups_remain(world):
    world.create_cluster(nodegroups=1)
    teardown = make_teardown(world)
    # Simulate EKS still deleting the node group: it stays listed after the delete call.
    teardown.eks = MagicMock(wraps=world.client("eks"))
    teardown.eks.exceptions = world.client("eks").exceptions
    teardown.eks.delete_nodegroup = MagicMock()
    teardown.eks.get_paginator = world.client("eks").get_paginator

    progress = teardown.run_pass(delete_cluster=True)

    assert world.cluster_exists()
    assert "cluster waits for its node groups" in progress.waiting
    assert not progress.done


def test_keep_cluster_leaves_the_cluster_for_opentofu(world):
    world.create_cluster(nodegroups=1)

    progress = run_to_completion(make_teardown(world), delete_cluster=False)

    assert progress.done
    assert world.cluster_exists()
    assert world.client("eks").list_nodegroups(clusterName=CLUSTER)["nodegroups"] == []


def test_deletes_available_project_volumes_only(world):
    pv = world.create_volume(tags={"project": PROJECT, "CSIVolumeName": "pvc-1"})
    foreign = world.create_volume(tags={"project": "other"})
    untagged = world.create_volume(tags={"Name": "scratch"})

    progress = run_to_completion(make_teardown(world), delete_cluster=False)

    assert world.volume_ids() == {foreign, untagged}
    assert f"volume:{pv}" in progress.deleted


def test_attached_persistent_volume_keeps_the_pass_waiting(world):
    ec2 = world.client("ec2")
    instance = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1, SubnetId=world.subnet_ids[0])["Instances"][0]
    pv = world.create_volume(tags={"project": PROJECT, "ebs.csi.aws.com/cluster": "true"})
    ec2.attach_volume(VolumeId=pv, InstanceId=instance["InstanceId"], Device="/dev/sdf")

    progress = make_teardown(world).run_pass(delete_cluster=False)

    assert pv in world.volume_ids()
    assert progress.waiting == ["1 persistent volume(s) still attached"]


def test_deletes_available_project_network_interfaces(world):
    ec2 = world.client("ec2")
    eni = ec2.create_network_interface(
        SubnetId=world.subnet_ids[0],
        TagSpecifications=[{"ResourceType": "network-interface", "Tags": [{"Key": "project", "Value": PROJECT}]}],
    )["NetworkInterface"]["NetworkInterfaceId"]

    progress = run_to_completion(make_teardown(world), delete_cluster=False)

    assert f"network-interface:{eni}" in progress.deleted
    assert eni not in {n["NetworkInterfaceId"] for n in ec2.describe_network_interfaces()["NetworkInterfaces"]}


def test_deletes_load_balancer_security_groups_after_the_cluster(world):
    ec2 = world.client("ec2")
    sg = ec2.create_security_group(
        GroupName="k8s-shop-gateway",
        Description="NLB",
        VpcId=world.vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": CLUSTER}, {"Key": "project", "Value": PROJECT}],
            }
        ],
    )["GroupId"]

    progress = run_to_completion(make_teardown(world), delete_cluster=True)

    assert progress.done
    assert f"security-group:{sg}" in progress.deleted


def test_dry_run_deletes_nothing(world):
    world.create_cluster(nodegroups=1)
    lb = world.create_load_balancer("k8s-shop-gateway", cluster=CLUSTER)
    vol = world.create_volume(tags={"project": PROJECT})

    progress = make_teardown(world, dry_run=True).run(delete_cluster=True, time_left=passes(), interval=0.5, sleep=lambda _: None)

    assert progress.dry_run
    assert f"load-balancer:{lb}" in progress.deleted and f"volume:{vol}" in progress.deleted
    assert lb in world.load_balancer_arns() and vol in world.volume_ids() and world.cluster_exists()


def test_access_denied_is_reported_not_swallowed(world):
    world.create_load_balancer("k8s-shop-gateway", cluster=CLUSTER)
    teardown = make_teardown(world)
    elbv2 = world.client("elbv2")
    teardown.elbv2 = MagicMock(wraps=elbv2)
    teardown.elbv2.get_paginator = elbv2.get_paginator
    teardown.elbv2.delete_load_balancer.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DeleteLoadBalancer"
    )

    progress = teardown.run(delete_cluster=False, time_left=passes(), interval=0.5, sleep=lambda _: None)

    assert not progress.done
    assert progress.errors and "AccessDenied" in progress.errors[0]


def test_unexpected_errors_propagate(world):
    world.create_load_balancer("k8s-shop-gateway", cluster=CLUSTER)
    teardown = make_teardown(world)
    elbv2 = world.client("elbv2")
    teardown.elbv2 = MagicMock(wraps=elbv2)
    teardown.elbv2.get_paginator = elbv2.get_paginator
    teardown.elbv2.delete_load_balancer.side_effect = ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "X")

    with pytest.raises(ClientError):
        teardown.run_pass(delete_cluster=False)


def test_stops_waiting_when_time_runs_out(world):
    world.create_cluster(nodegroups=1)
    teardown = make_teardown(world)
    teardown.eks = MagicMock(wraps=world.client("eks"))
    teardown.eks.exceptions = world.client("eks").exceptions
    teardown.eks.get_paginator = world.client("eks").get_paginator
    teardown.eks.delete_nodegroup = MagicMock()
    sleeps: list[float] = []

    progress = teardown.run(delete_cluster=True, time_left=lambda: 5, interval=30, sleep=sleeps.append)

    assert not progress.done and sleeps == []
