output "alb_dns_name" {
  description = "Internal ALB DNS name -- resolvable within the VPC, used directly as AUTHZ_SERVICE_URL (no API Gateway in front)."
  value       = aws_lb.this.dns_name
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "service_name" {
  value = aws_ecs_service.this.name
}

output "task_definition_family" {
  value = aws_ecs_task_definition.this.family
}

output "security_group_id" {
  value = aws_security_group.service.id
}
