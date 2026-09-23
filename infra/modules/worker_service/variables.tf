variable "name_prefix" {
  description = "Prefix applied to resource names, e.g. \"gateway-dev-worker\"."
  type        = string
}

variable "environment" {
  description = "Environment tag, e.g. \"dev\" or \"prod\"."
  type        = string
}

variable "aws_region" {
  description = "AWS region (used for the CloudWatch log driver config)."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "cluster_name" {
  description = "Existing ECS cluster to run this service in -- the worker shares the gateway-api service's cluster (ecs_service module's cluster_name output) rather than getting its own, since there's no reason to pay for/manage a second cluster per environment."
  type        = string
}

variable "image" {
  description = "Full image URI (ECR repo URL + tag). Same image as the gateway-api service -- the worker is the same codebase run with a different command, not a separately built artifact."
  type        = string
}

variable "command" {
  description = "Container command override, e.g. [\"python\", \"-m\", \"services.worker.main\"]."
  type        = list(string)
}

variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "enable_execute_command" {
  type    = bool
  default = false
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

variable "container_env" {
  type    = map(string)
  default = {}
}

variable "bedrock_model_ids" {
  description = "Model/inference-profile IDs the task role may invoke, matching policies/route_sets.yaml."
  type        = list(string)
}

variable "bedrock_profile_regions" {
  type    = list(string)
  default = ["us-east-1", "us-east-2", "us-west-2"]
}

variable "sqs_queue_arn" {
  description = "Jobs queue this worker consumes from."
  type        = string
}

variable "sqs_queue_name" {
  description = "Same queue as sqs_queue_arn, by name -- CloudWatch alarm dimensions need the name, not the ARN (plan section 35.6)."
  type        = string
}

variable "autoscaling_min_capacity" {
  type    = number
  default = 1
}

variable "autoscaling_max_capacity" {
  type    = number
  default = 6
}

# Plan section 35's P1 hardening -- same jobs_dlq queue name this
# worker's own redrive_policy already points failed jobs at
# (environments/*/main.tf), needed as its own input since this module
# only receives the live queue's ARN/name above, not the DLQ's.
variable "sqs_dlq_name" {
  type = string
}

variable "sns_topic_arn" {
  type    = string
  default = null
}

variable "dynamodb_table_arn" {
  description = "Job records table this worker reads/writes."
  type        = string
}

# M8 FinOps: the worker only writes (records spend after a job
# succeeds) -- budget is checked once, at submission, by gateway-api;
# re-checking it here would let a burst of already-queued jobs still
# blow through a budget that was fine at each one's submission time,
# which is a real gap but out of scope for the M7/M8 minimal-viable cut.
variable "usage_table_arn" {
  type = string
}

variable "tenant_policies_table_arn" {
  type = string
}
variable "admission_control_table_arn" {
  type = string
}
variable "guardrail_arn" {
  type = string
}
