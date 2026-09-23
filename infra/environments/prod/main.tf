terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix = "gateway-prod"
}

module "network" {
  source = "../../modules/network"

  name_prefix = local.name_prefix
  environment = "prod"
  aws_region  = var.aws_region
}

module "ecr" {
  source = "../../modules/ecr"

  repository_name = local.name_prefix
  environment     = "prod"
}

# API Gateway (the VPC Link's ENIs, and everything else under
# modules/api_gateway) moved out to the platform-edge-gateway repo --
# see plan.md Section 25. This looks its security group up by name
# (never a cross-repo state reference) so ecs_service's ALB can allow
# it as ingress regardless of which repo created it. Not yet real for
# prod -- platform-edge-gateway's own environments/prod hasn't been
# applied yet (prod is held, same standing pattern as everything
# else); this data source will fail to resolve until it has been.
data "aws_security_group" "api_gateway_vpc_link" {
  name   = "${local.name_prefix}-api-gw-vpc-link"
  vpc_id = module.network.vpc_id
}

module "ecs_service" {
  source = "../../modules/ecs_service"

  name_prefix                = local.name_prefix
  environment                = "prod"
  aws_region                 = var.aws_region
  vpc_id                     = module.network.vpc_id
  private_subnet_ids         = module.network.private_subnet_ids
  vpc_link_security_group_id = data.aws_security_group.api_gateway_vpc_link.id
  log_group_name             = "/ai-platform/ecs/bedrock-gateway-prod"

  # No image has been pushed on a first apply -- CI registers the real
  # task definition revision on its first deploy (see infra/README.md).
  # The service will show 0 running tasks until then; expected.
  image = "${module.ecr.repository_url}:bootstrap"

  desired_count = var.desired_count
  task_cpu      = var.task_cpu
  task_memory   = var.task_memory

  bedrock_model_ids = var.bedrock_model_ids

  jobs_queue_arn              = aws_sqs_queue.jobs.arn
  jobs_table_arn              = aws_dynamodb_table.jobs.arn
  usage_table_arn             = aws_dynamodb_table.usage.arn
  admission_control_table_arn = aws_dynamodb_table.admission_control.arn
  audit_bucket_arn            = aws_s3_bucket.audit.arn
  audit_kms_key_arn           = aws_kms_key.audit.arn
  request_audit_bucket_arn    = aws_s3_bucket.request_audit.arn
  bedrock_guardrail_arn       = aws_bedrock_guardrail.this.guardrail_arn

  onboarding_requests_table_arn            = aws_dynamodb_table.onboarding_requests.arn
  onboarding_audit_table_arn               = aws_dynamodb_table.onboarding_audit.arn
  provisioned_tenant_policies_table_arn    = aws_dynamodb_table.provisioned_tenant_policies.arn
  provisioned_principal_mappings_table_arn = aws_dynamodb_table.provisioned_principal_mappings.arn

  policy_change_requests_table_arn              = aws_dynamodb_table.policy_change_requests.arn
  provisioned_tenant_policies_history_table_arn = aws_dynamodb_table.provisioned_tenant_policies_history.arn

  container_env = {
    AWS_REGION                   = var.aws_region
    BEDROCK_MODEL_ID             = var.bedrock_model_ids[0]
    GATEWAY_HOST                 = "0.0.0.0"
    GATEWAY_PORT                 = "8080"
    SERVICE_NAME                 = local.name_prefix
    ENVIRONMENT                  = "prod"
    LOG_LEVEL                    = "INFO"
    ROUTE_SET_CONFIG_PATH        = "policies/route_sets.yaml"
    TENANT_POLICY_PATH           = "policies/tenants.yaml"
    IAM_TENANTS_PATH             = "policies/iam_tenants.yaml"
    JOBS_QUEUE_URL               = aws_sqs_queue.jobs.url
    JOBS_TABLE_NAME              = aws_dynamodb_table.jobs.name
    USAGE_TABLE_NAME             = aws_dynamodb_table.usage.name
    ADMISSION_CONTROL_TABLE_NAME = aws_dynamodb_table.admission_control.name
    AUDIT_BUCKET_NAME            = aws_s3_bucket.audit.id
    REQUEST_AUDIT_BUCKET_NAME    = aws_s3_bucket.request_audit.id
    BEDROCK_GUARDRAIL_ID         = aws_bedrock_guardrail.this.guardrail_id
    BEDROCK_GUARDRAIL_VERSION    = aws_bedrock_guardrail_version.v1.version
    # Plan section 35.3 (P0 production hardening): dev stays fail-open
    # (unset -> Settings default False) since its model_registry.yaml/
    # tenant data_classification coverage is intentionally partial --
    # prod opts into fail-closed since a real rollout should already
    # have curated both before going live.
    MODEL_GOVERNANCE_FAIL_CLOSED = "true"

    ONBOARDING_REQUESTS_TABLE_NAME            = aws_dynamodb_table.onboarding_requests.name
    ONBOARDING_AUDIT_TABLE_NAME               = aws_dynamodb_table.onboarding_audit.name
    PROVISIONED_TENANT_POLICIES_TABLE_NAME    = aws_dynamodb_table.provisioned_tenant_policies.name
    PROVISIONED_PRINCIPAL_MAPPINGS_TABLE_NAME = aws_dynamodb_table.provisioned_principal_mappings.name

    POLICY_CHANGE_REQUESTS_TABLE_NAME              = aws_dynamodb_table.policy_change_requests.name
    PROVISIONED_TENANT_POLICIES_HISTORY_TABLE_NAME = aws_dynamodb_table.provisioned_tenant_policies_history.name
  }
}

# --- M7: async jobs -----------------------------------------------------

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${local.name_prefix}-jobs-dlq"
  message_retention_seconds = 1209600 # 14 days

  tags = {
    Environment = "prod"
  }
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${local.name_prefix}-jobs"
  visibility_timeout_seconds = 60 # must exceed the worker's expected per-job processing time

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = "prod"
  }
}

resource "aws_dynamodb_table" "jobs" {
  # Renamed 2026-09-23 to match dev's gateway-jobs-dev -- see
  # environments/dev/main.tf's own comment on this table for the
  # migration procedure dev went through; prod is config-mirrored, never
  # terraform-applied (no live prod table exists yet), so this is a
  # plain source edit, not a live cutover.
  name         = "gateway-jobs-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

# Plan section 35.12 -- see environments/dev/main.tf's own copy of
# this resource for the full design note.
resource "aws_kms_key" "audit" {
  description         = "${local.name_prefix} audit buckets (S3AuditStore, S3RequestAuditStore)"
  enable_key_rotation = true

  tags = {
    Environment = "prod"
  }
}

resource "aws_kms_alias" "audit" {
  name          = "alias/${local.name_prefix}-audit"
  target_key_id = aws_kms_key.audit.key_id
}

# --- Audit payload store (S3AuditStore, services/gateway/telemetry/
# debug_capture.py) -- one redacted JSON object per captured request,
# opt-in per tenant (debug_capture_enabled). Encrypted, public access
# fully blocked, one uniform lifecycle expiration for everyone -- true
# per-tenant retention (TenantPolicy.debug_capture_retention_days)
# isn't enforced yet, see that field's docstring for why.
resource "aws_s3_bucket" "audit" {
  bucket = "${local.name_prefix}-audit"

  tags = {
    Environment = "prod"
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket = aws_s3_bucket.audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.audit.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "expire-all-objects"
    status = "Enabled"
    filter {}

    expiration {
      days = 30
    }
  }
}

# --- Request audit store (S3RequestAuditStore, services/gateway/
# telemetry/request_audit.py, plan section 34.4) -- SEPARATE from the
# audit bucket above: metadata-only, always on, Object Lock enabled.
#
# Unlike dev's copy of this resource (GOVERNANCE mode, 90 days -- see
# that comment for why), prod deliberately uses COMPLIANCE mode with a
# genuine regulatory retention (7 years / 2555 days, typical for
# insurance) -- the real decision this whole environment exists to
# represent but never applies (see this file's/plan.md's standing
# "prod is config-mirrored, never terraform-applied" note). COMPLIANCE
# mode means literally nobody, including account root, can delete an
# object before retention expires -- appropriate for prod's actual
# regulatory audit trail, deliberately NOT the default for dev where
# it would just be a footgun.
resource "aws_s3_bucket" "request_audit" {
  bucket              = "${local.name_prefix}-audit-immutable"
  object_lock_enabled = true

  tags = {
    Environment = "prod"
  }
}

resource "aws_s3_bucket_versioning" "request_audit" {
  bucket = aws_s3_bucket.request_audit.id

  versioning_configuration {
    status = "Enabled" # required for Object Lock
  }
}

resource "aws_s3_bucket_public_access_block" "request_audit" {
  bucket = aws_s3_bucket.request_audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "request_audit" {
  bucket = aws_s3_bucket.request_audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.audit.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "request_audit" {
  bucket = aws_s3_bucket.request_audit.id

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 2555
    }
  }

  depends_on = [aws_s3_bucket_versioning.request_audit]
}

# --- Bedrock Guardrail (BedrockGuardrailClient, services/gateway/
# guardrails/bedrock_guardrail.py) -- real ML-based moderation,
# replacing BasicGuardrailClient's regex checks. Scope deliberately
# mirrors what BasicGuardrailClient already covered (SSN/card/email PII,
# prompt-injection) rather than inventing a broader content policy --
# one shared guardrail for every tenant today, see
# bedrock_guardrail.py's own scoping note on why per-policy guardrails
# aren't built yet.
resource "aws_bedrock_guardrail" "this" {
  name = "${local.name_prefix}-guardrail"
  # Explicit, not left unset -- an unset description tripped a real
  # provider bug live in dev ("Provider returned invalid result object
  # after apply ... unknown value for ... description"), leaving the
  # resource tainted after an otherwise-successful create.
  description               = "Guardrail for BedrockGuardrailClient (services/gateway/guardrails/bedrock_guardrail.py) -- SSN/card/email PII + prompt-attack filtering."
  blocked_input_messaging   = "This input was blocked by a content guardrail."
  blocked_outputs_messaging = "This response was blocked by a content guardrail."

  sensitive_information_policy_config {
    pii_entities_config {
      type   = "US_SOCIAL_SECURITY_NUMBER"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "CREDIT_DEBIT_CARD_NUMBER"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "EMAIL"
      action = "BLOCK"
    }
  }

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "MEDIUM"
      output_strength = "NONE"
    }
  }

  tags = {
    Environment = "prod"
  }
}

# Plan section 35's P1 hardening -- see environments/dev/main.tf's own
# copy of this resource for the full design note on immutability and
# how to publish a new version.
resource "aws_bedrock_guardrail_version" "v1" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Initial published version -- SSN/card/email PII + prompt-attack filtering."
  skip_destroy  = true
}

# --- M8: FinOps -----------------------------------------------------------

resource "aws_dynamodb_table" "usage" {
  # Renamed 2026-09-23 to match dev's gateway-usage-dev -- see
  # environments/dev/main.tf's own comment on this table.
  name         = "gateway-usage-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"
  range_key    = "month"

  attribute {
    name = "tenant_id"
    type = "S"
  }
  attribute {
    name = "month"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

# Plan section 35.2: distributed admission control -- see
# environments/dev/main.tf's own copy of this resource for the full
# design note.
resource "aws_dynamodb_table" "admission_control" {
  # Renamed 2026-09-23 to match dev's gateway-admission-control-dev --
  # see environments/dev/main.tf's own comment on this table.
  name         = "gateway-admission-control-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Plan section 35.18 -- see environments/dev/main.tf's own copy of
  # this comment for the full design note.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Environment = "prod"
  }
}

# --- M11: Application Onboarding (plan section 22) -------------------------

resource "aws_dynamodb_table" "onboarding_requests" {
  # Renamed 2026-09-23 to match dev's gateway-onboarding-requests-dev --
  # see environments/dev/main.tf's own comment on this table.
  name         = "gateway-onboarding-requests-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"

  attribute {
    name = "request_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

resource "aws_dynamodb_table" "onboarding_audit" {
  # Renamed 2026-09-23 to match dev's gateway-onboarding-audit-dev --
  # see environments/dev/main.tf's own comment on this table.
  name         = "gateway-onboarding-audit-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"
  range_key    = "timestamp"

  attribute {
    name = "request_id"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "N"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

resource "aws_dynamodb_table" "provisioned_tenant_policies" {
  # Renamed 2026-09-23 to match dev's gateway-tenant-policies-dev -- see
  # environments/dev/main.tf's own comment on this table.
  name         = "gateway-tenant-policies-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"

  attribute {
    name = "tenant_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

resource "aws_dynamodb_table" "provisioned_principal_mappings" {
  name         = "${local.name_prefix}-provisioned-principal-mappings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "principal_arn"

  attribute {
    name = "principal_arn"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

# --- Plan section 33: policy versioning/approval/rollback -----------------

resource "aws_dynamodb_table" "policy_change_requests" {
  # Renamed 2026-09-23 to match dev's gateway-policy-change-requests-dev
  # -- see environments/dev/main.tf's own comment on this table.
  name         = "gateway-policy-change-requests-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "change_id"

  attribute {
    name = "change_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

resource "aws_dynamodb_table" "provisioned_tenant_policies_history" {
  # Renamed 2026-09-23 to match dev's
  # gateway-tenant-policies-history-dev -- see
  # environments/dev/main.tf's own comment on this table.
  name         = "gateway-tenant-policies-history-prod"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"
  range_key    = "policy_epoch"

  attribute {
    name = "tenant_id"
    type = "S"
  }
  attribute {
    name = "policy_epoch"
    type = "N"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = "prod"
  }
}

module "worker_service" {
  source = "../../modules/worker_service"

  name_prefix        = "${local.name_prefix}-worker"
  environment        = "prod"
  aws_region         = var.aws_region
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  cluster_name       = module.ecs_service.cluster_name
  log_group_name     = "/ai-platform/ecs/bedrock-gateway-worker-prod"

  # Same image as gateway-api -- same codebase, different command.
  image   = "${module.ecr.repository_url}:bootstrap"
  command = ["python", "-m", "services.worker.main"]

  desired_count = 1
  task_cpu      = var.task_cpu
  task_memory   = var.task_memory

  bedrock_model_ids  = var.bedrock_model_ids
  sqs_queue_arn      = aws_sqs_queue.jobs.arn
  sqs_queue_name     = aws_sqs_queue.jobs.name
  sqs_dlq_name       = aws_sqs_queue.jobs_dlq.name
  dynamodb_table_arn = aws_dynamodb_table.jobs.arn
  usage_table_arn    = aws_dynamodb_table.usage.arn

  tenant_policies_table_arn   = aws_dynamodb_table.provisioned_tenant_policies.arn
  admission_control_table_arn = aws_dynamodb_table.admission_control.arn
  guardrail_arn               = aws_bedrock_guardrail.this.guardrail_arn

  container_env = {
    PROVISIONED_TENANT_POLICIES_TABLE_NAME = aws_dynamodb_table.provisioned_tenant_policies.name
    ADMISSION_CONTROL_TABLE_NAME           = aws_dynamodb_table.admission_control.name
    BEDROCK_GUARDRAIL_ID                   = aws_bedrock_guardrail.this.guardrail_id
    BEDROCK_GUARDRAIL_VERSION              = aws_bedrock_guardrail_version.v1.version
    AWS_REGION                             = var.aws_region
    BEDROCK_MODEL_ID                       = var.bedrock_model_ids[0]
    SERVICE_NAME                           = "${local.name_prefix}-worker"
    SERVICE                                = "bedrock-gateway-worker"
    ENVIRONMENT                            = "prod"
    LOG_LEVEL                              = "INFO"
    ROUTE_SET_CONFIG_PATH                  = "policies/route_sets.yaml"
    TENANT_POLICY_PATH                     = "policies/tenants.yaml"
    IAM_TENANTS_PATH                       = "policies/iam_tenants.yaml"
    JOBS_QUEUE_URL                         = aws_sqs_queue.jobs.url
    JOBS_TABLE_NAME                        = aws_dynamodb_table.jobs.name
    USAGE_TABLE_NAME                       = aws_dynamodb_table.usage.name
  }
}

# --- M10: self-service portal -- moved to platform-control-plane
# (Phase 1 of the platform restructuring, portal/cognito infra
# ownership). See that repo's own infra/environments/prod for the
# real module block; environments/dev/main.tf here has carried no
# portal_service/ecr_portal reference for the same reason since the
# actual cutover -- prod's copy just hadn't been cleaned up yet.
