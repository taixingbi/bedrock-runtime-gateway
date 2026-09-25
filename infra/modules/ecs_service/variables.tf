variable "name_prefix" {
  description = "Prefix applied to resource names, e.g. \"gateway-dev\"."
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

variable "vpc_link_security_group_id" {
  description = "Security group of the API Gateway VPC Link that's the ALB's only allowed ingress source (the ALB is private -- API Gateway now lives in the separate platform-edge-gateway repo, looked up by name at the caller, not referenced directly)."
  type        = string
}

variable "image" {
  description = "Full image URI (ECR repo URL + tag) to run. Placeholder on first apply -- CI overwrites it on every deploy via a new task definition revision."
  type        = string
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "listener_port" {
  type    = number
  default = 80
}

# Plan section 35.5 (P1 production hardening): an HTTPS listener is
# added ALONGSIDE the existing HTTP one (below), not a replacement --
# platform-edge-gateway's VPC Link integration still points at port 80
# today, and cutting that over needs its own verification (does
# API Gateway's VPC_LINK-to-ALB integration actually trust a listener
# cert issued by this private CA the way gateway-api's own pinned
# HTTP client does for authz-service's equivalent hop -- unconfirmed,
# not something to guess at on this platform's primary live traffic
# path). This makes the HTTPS listener available; the cutover
# (deleting the HTTP listener, repointing platform-edge-gateway at 443)
# is a deliberate follow-up, not done here.
variable "https_listener_port" {
  type    = number
  default = 443
}

variable "private_ca_arn" {
  description = "ACM Private CA to issue this ALB's HTTPS listener certificate from -- must already be ACTIVE (pass the activation resource's own ARN, not the bare CA's). Optional and null by default -- unlike modules/authz_service (only ever wired into dev), this module is also called from environments/prod, which has no private CA of its own yet (creating one is a real ~$400/mo decision, deliberately not made on prod's behalf here). null skips the HTTPS listener/cert entirely; only the existing HTTP listener exists in that case, same as before this variable existed."
  type        = string
  default     = null
}

# Plan section 35's P1 hardening -- notification target for this
# service's CloudWatch alarms. Optional; alarms are still created with
# no actions if unset (visible in the console, just silent).
variable "sns_topic_arn" {
  type    = string
  default = null
}

variable "task_cpu" {
  description = "Fargate task CPU units (256 = 0.25 vCPU)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 1024
}

variable "desired_count" {
  type    = number
  default = 1
}

# Plan section 35.6: the service's own desired_count above is only the
# INITIAL value (first apply, or if autoscaling briefly can't reach
# the target) -- aws_appautoscaling_target owns it afterward. min=2
# for real HA (a single task is a single point of failure no autoscaling
# policy protects against); max is a cost ceiling, not a load estimate.
variable "autoscaling_min_capacity" {
  type    = number
  default = 2
}

variable "autoscaling_max_capacity" {
  type    = number
  default = 6
}

variable "enable_execute_command" {
  description = "Allow `aws ecs execute-command` into running tasks -- debugging convenience, leave false in prod."
  type        = bool
  default     = false
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
  description = "Environment variables passed to the gateway-api container."
  type        = map(string)
  default     = {}
}

# mTLS cutover (plan section 35): the client cert/key gateway-api
# presents to authz-service's ALB shouldn't be a plain container_env
# value (CloudWatch Logs / `aws ecs describe-task-definition` would
# both expose a private key in plaintext) -- ECS resolves each of
# these from Secrets Manager at container start and injects it as a
# real environment variable the running process reads, same end
# result as container_env from the app's own point of view, but never
# persisted in the task definition or logs.
variable "container_secrets" {
  description = "Environment variables resolved from Secrets Manager at container start -- map of env var name to secret ARN (a full ARN, or ARN:jsonKey for one field of a JSON secret)."
  type        = map(string)
  default     = {}
}

variable "bedrock_model_ids" {
  description = "Model/inference-profile IDs the task role may invoke, matching policies/route_sets.yaml (e.g. \"us.amazon.nova-micro-v1:0\")."
  type        = list(string)
}

variable "bedrock_profile_regions" {
  description = "Regions a \"us.\"-prefixed cross-region inference profile can route to; the underlying foundation-model ARN in each must also be authorized."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}

# Required, not optional: every current caller already has a jobs
# queue/table (M7), and an optional-with-count design here hits a real
# Terraform limitation -- see main.tf's jobs_access comment. The task
# role gets permission to enqueue jobs and read/write their status;
# this is the gateway-api side of the pair, worker_service's task role
# is the consumer side.
variable "jobs_queue_arn" {
  type = string
}

variable "jobs_table_arn" {
  type = string
}

variable "audit_bucket_arn" {
  type = string
}

# Plan section 35.12 (P2 production hardening) -- both audit buckets'
# shared SSE-KMS key. Writing/reading an object under a customer-
# managed key needs explicit kms:GenerateDataKey*/kms:Decrypt beyond
# the existing s3:PutObject/GetObject grants (SSE-S3's AES256, what
# both buckets used before this, needed no such grant).
variable "audit_kms_key_arn" {
  type = string
}

# Plan section 34.4: separate bucket from audit_bucket_arn above --
# metadata-only, always-on, Object Lock enabled.
variable "request_audit_bucket_arn" {
  type = string
}

variable "bedrock_guardrail_arn" {
  type = string
}

# M8 FinOps: gateway-api both reads (budget check before calling the
# model) and writes (record spend after a successful response) this
# table -- see usage/store.py's UsageStore. UpdateItem, not PutItem:
# add_and_get() always does an atomic ADD via UpdateItem, never a full
# overwrite.
variable "usage_table_arn" {
  type = string
}

# Plan section 35.2 -- backs both DynamoDbConcurrencyLimiter and
# DynamoDbRateLimiter.
variable "admission_control_table_arn" {
  type = string
}

# routing/model_quota.py -- per-model AWS Bedrock quota (rpm_limit/
# tpm_limit) pulled from AWS Service Quotas by
# scripts/sync_model_quotas_from_aws.py. A separate table from
# admission_control_table_arn above (not "one table, no reason to
# provision two" like that one) because this table's rows are written
# out-of-band by that sync script, not just live request-time
# counters -- see config.py's own note on this distinction.
variable "model_quotas_table_arn" {
  type = string
}

# ModelQuotaLimiter's own live rate-limit counter rows (DynamoDbRateLimiter's
# CAS token buckets, same class tenants use) -- a SEPARATE table from
# model_quotas_table_arn above, not a differently-prefixed row in it (see
# config.py's own note on why: keeps model_quotas_table_arn provably
# quota-config-only, never mixed with live counters).
variable "model_ratelimits_table_arn" {
  type = string
}

# M11 Application Onboarding (plan section 22) -- all four required,
# same "known after apply on first create" reasoning as
# jobs_queue_arn/jobs_table_arn/usage_table_arn above.
variable "onboarding_requests_table_arn" {
  type = string
}

variable "onboarding_audit_table_arn" {
  type = string
}

variable "provisioned_tenant_policies_table_arn" {
  type = string
}

variable "provisioned_principal_mappings_table_arn" {
  type = string
}

# Plan section 33: policy versioning/approval/rollback -- same "known
# after apply on first create" reasoning as the M11 pair above.
variable "policy_change_requests_table_arn" {
  type = string
}

variable "provisioned_tenant_policies_history_table_arn" {
  type = string
}
