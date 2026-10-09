output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "API endpoint (reachable only from operator_cidr)."
  value       = aws_eks_cluster.this.endpoint
}

output "kubernetes_version" {
  description = "Running Kubernetes version."
  value       = aws_eks_cluster.this.version
}

output "node_group_name" {
  description = "Spot node group scaled by cloud-pause/resume."
  value       = aws_eks_node_group.spot.node_group_name
}

output "vpc_id" {
  description = "VPC ID passed to the LB controller (IMDS is closed to pods)."
  value       = data.aws_vpc.this.id
}
