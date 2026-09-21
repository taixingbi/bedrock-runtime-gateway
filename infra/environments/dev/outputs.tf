output "alb_dns_name" {
  description = "The ALB is private -- only reachable from inside the VPC (e.g. via ECS Exec), not the internet. Use api_gateway_url for real traffic."
  value       = module.ecs_service.alb_dns_name
}

# api_gateway_url / execute_api_arn_iam_route moved to the
# platform-edge-gateway repo's own outputs (plan.md Section 25) -- this
# repo no longer owns that resource.

output "ecr_repository_url" {
  value = module.ecr.repository_url
}

output "ecs_cluster_name" {
  value = module.ecs_service.cluster_name
}

output "ecs_service_name" {
  value = module.ecs_service.service_name
}

output "jobs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "jobs_table_name" {
  value = aws_dynamodb_table.jobs.name
}

output "worker_service_name" {
  value = module.worker_service.service_name
}

output "usage_table_name" {
  value = aws_dynamodb_table.usage.name
}
