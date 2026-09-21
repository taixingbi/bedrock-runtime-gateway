variable "name_prefix" {
  description = "Prefix applied to resource names, e.g. \"gateway-dev-authz\"."
  type        = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "caller_security_group_id" {
  description = "Security group of the ECS service allowed to call this one -- gateway-api's task SG (modules/ecs_service). Nothing else may reach the ALB; this is a purely internal service, not fronted by API Gateway."
  type        = string
}

variable "control_plane_caller_security_group_id" {
  description = "Phase 4 (2026-09-21, \"direct cutover\"): platform-control-plane's backend_service task SG, also allowed to call this ALB directly. Nullable/optional so environments without that backend live yet (prod today) don't need to pass it."
  type        = string
  default     = null
}

variable "image" {
  type = string
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "task_cpu" {
  type    = number
  default = 256
}

variable "task_memory" {
  type    = number
  default = 512
}

variable "desired_count" {
  type    = number
  default = 2 # plan section 35.6 -- real HA floor
}

variable "autoscaling_min_capacity" {
  type    = number
  default = 2
}

variable "autoscaling_max_capacity" {
  type    = number
  default = 4
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "log_group_name" {
  description = "Override for the CloudWatch log group name; defaults to \"/ecs/<name_prefix>\" when empty."
  type        = string
  default     = ""
}

variable "private_ca_arn" {
  description = "ACM Private CA to issue this ALB's HTTPS listener certificate from -- must already be ACTIVE (pass the activation resource's own ARN, not the bare CA's, so Terraform sequences correctly)."
  type        = string
}

variable "sns_topic_arn" {
  type    = string
  default = null
}

variable "container_env" {
  type    = map(string)
  default = {}
}

variable "provisioned_principal_mappings_table_arn" {
  description = "Read-only: the same onboarding-provisioned principal-mappings table this repo's app/ half's task role writes to (modules/ecs_service). This service only reads it."
  type        = string
}
