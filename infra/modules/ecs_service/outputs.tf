output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "alb_listener_arn" {
  description = "For API Gateway's VPC Link private integration to target."
  value       = aws_lb_listener.http.arn
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

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}

output "task_security_group_id" {
  description = "M12: the gateway-api task's own SG, so modules/authz_service can scope its ALB ingress to exactly this caller and nothing else."
  value       = aws_security_group.service.id
}
