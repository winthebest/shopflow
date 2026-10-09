output "vpc_id" {
  description = "VPC of the shopflow cluster (the LB controller needs it explicitly because IMDS is closed to pods)."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Public subnet per AZ suffix (control plane uses all of them)."
  value       = { for k, s in aws_subnet.public : k => s.id }
}

output "node_subnet_id" {
  description = "The only subnet where nodes (and their EBS volumes) live."
  value       = one([for s in aws_subnet.public : s.id if s.availability_zone == local.node_az])
}
