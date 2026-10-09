"""Moto-backed AWS world for the reaper tests: every call stays in memory, never reaches AWS."""

from __future__ import annotations

from dataclasses import dataclass

import boto3

REGION = "ap-southeast-1"
CLUSTER = "shopflow"
PROJECT = "shopflow"
LEASE_PARAM = "/shopflow/aws/control/lease-expires-at"


@dataclass
class World:
    session: boto3.session.Session
    vpc_id: str
    subnet_ids: list[str]

    def client(self, name: str):
        return self.session.client(name)

    def create_cluster(self, *, nodegroups: int = 1) -> None:
        eks = self.client("eks")
        role = "arn:aws:iam::123456789012:role/shopflow-eks-cluster"
        eks.create_cluster(
            name=CLUSTER,
            roleArn=role,
            resourcesVpcConfig={"subnetIds": self.subnet_ids},
            tags={"project": PROJECT},
        )
        for i in range(nodegroups):
            eks.create_nodegroup(
                clusterName=CLUSTER,
                nodegroupName=f"shopflow-spot-{i}",
                nodeRole="arn:aws:iam::123456789012:role/shopflow-eks-node",
                subnets=self.subnet_ids[:1],
                tags={"project": PROJECT},
            )

    def create_load_balancer(self, name: str, *, cluster: str | None) -> str:
        tags = [{"Key": "project", "Value": PROJECT}]
        if cluster:
            tags.append({"Key": "elbv2.k8s.aws/cluster", "Value": cluster})
        lb = self.client("elbv2").create_load_balancer(Name=name, Subnets=self.subnet_ids, Type="network", Tags=tags)
        return lb["LoadBalancers"][0]["LoadBalancerArn"]

    def create_target_group(self, name: str, *, cluster: str | None) -> str:
        tags = [{"Key": "elbv2.k8s.aws/cluster", "Value": cluster}] if cluster else [{"Key": "Name", "Value": name}]
        tg = self.client("elbv2").create_target_group(Name=name, Protocol="TCP", Port=443, VpcId=self.vpc_id, Tags=tags)
        return tg["TargetGroups"][0]["TargetGroupArn"]

    def create_volume(self, *, tags: dict[str, str]) -> str:
        vol = self.client("ec2").create_volume(
            AvailabilityZone=f"{REGION}a",
            Size=10,
            TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}],
        )
        return vol["VolumeId"]

    def volume_ids(self) -> set[str]:
        return {v["VolumeId"] for v in self.client("ec2").describe_volumes()["Volumes"]}

    def load_balancer_arns(self) -> set[str]:
        return {lb["LoadBalancerArn"] for lb in self.client("elbv2").describe_load_balancers()["LoadBalancers"]}

    def cluster_exists(self) -> bool:
        return CLUSTER in self.client("eks").list_clusters()["clusters"]
