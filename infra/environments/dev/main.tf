terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix = "gateway-dev"

  # Plan section 35.5 -- the shared internal Private CA, owned by
  # platform-foundation (Phase 3c, 2026-09-21). Hardcoded, not a
  # resource reference -- ACM PCA has no clean "look up by name" data
  # source, and this ARN is stable for the CA's lifetime. Update this
  # if the CA is ever destroyed and recreated (a new one gets a new
  # ARN) -- see platform-foundation's own environments/dev for the
  # owning resource.
  private_ca_arn = "arn:aws:acm-pca:us-east-1:646821141010:certificate-authority/328ba585-7400-4565-bad3-6bfda5c0196d"
}

# --- Plan section 35's P1 hardening: operational alarms -----------------
#
# One shared topic for every CloudWatch alarm across every service in
# this environment -- no subscription configured (that's a real
# decision -- who gets paged, an email vs. a real on-call/PagerDuty
# integration -- deliberately left to the user rather than guessed at
# here); the topic and the alarms feeding it exist and are real,
# subscribing to it is a one-line follow-up whenever there's a real
# destination to point it at.
resource "aws_sns_topic" "ops_alerts" {
  name = "${local.name_prefix}-ops-alerts"

  tags = {
    Environment = "dev"
  }
}

# Plan section 3 (2026-09-21): the VPC/subnets moved to
# platform-foundation -- looked up by name here, same loose-coupling
# convention as every other cross-repo lookup in this platform
# (data.aws_lb.authz below, platform-authz-service's own ALB lookup,
# etc.), rather than a module reference. This repo no longer owns any
# part of the network's lifecycle.
data "aws_vpc" "foundation" {
  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

data "aws_subnets" "foundation_private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.foundation.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${local.name_prefix}-private-*"]
  }
}

module "ecr" {
  source = "../../modules/ecr"

  repository_name = local.name_prefix
  environment     = "dev"
}

# API Gateway (the VPC Link's ENIs, and everything else under
# modules/api_gateway) moved out to the platform-edge-gateway repo --
# see plan.md Section 25. This looks its security group up by name
# (never a cross-repo state reference) so ecs_service's ALB can allow
# it as ingress regardless of which repo created it.
data "aws_security_group" "api_gateway_vpc_link" {
  name   = "${local.name_prefix}-api-gw-vpc-link"
  vpc_id = data.aws_vpc.foundation.id
}

# Plan section 36: authz_service's own Terraform (ECS/ALB/ECR) moved
# to platform-authz-service -- looked up by name here, same loose-
# coupling convention as api_gateway_vpc_link above, rather than a
# module reference. This repo keeps owning the resources authz-service
# only ever *reads* (the private CA, the ops_alerts SNS topic, the
# provisioned_principal_mappings table) -- see that repo's own
# environments/dev/main.tf for the full design note.
data "aws_lb" "authz" {
  name = "${local.name_prefix}-authz-alb"
}

# --- mTLS client certificate for calling authz-service (plan section
# 35, P1 production hardening) -------------------------------------
#
# Issued from the same private CA authz-service's own ALB listener
# cert comes from (local.private_ca_arn) -- ACM PCA only ever sees the
# CSR (a public key + identity); the private key is generated here and
# never leaves this Terraform run except into Secrets Manager
# (encrypted at rest), read by the running container via
# container_secrets below, never written to the task definition or
# CloudWatch Logs in plaintext.
resource "tls_private_key" "authz_client" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "authz_client" {
  private_key_pem = tls_private_key.authz_client.private_key_pem

  subject {
    common_name = "gateway-api.internal"
  }
}

resource "aws_acmpca_certificate" "authz_client" {
  certificate_authority_arn   = local.private_ca_arn
  certificate_signing_request = tls_cert_request.authz_client.cert_request_pem
  signing_algorithm           = "SHA256WITHRSA"
  template_arn                = "arn:aws:acm-pca:::template/EndEntityClientAuthCertificate/V1"

  validity {
    type  = "YEARS"
    value = 1
  }
}

resource "aws_secretsmanager_secret" "authz_client_cert" {
  name = "${local.name_prefix}-authz-client-cert"
}

resource "aws_secretsmanager_secret_version" "authz_client_cert" {
  secret_id     = aws_secretsmanager_secret.authz_client_cert.id
  secret_string = aws_acmpca_certificate.authz_client.certificate
}

resource "aws_secretsmanager_secret" "authz_client_key" {
  name = "${local.name_prefix}-authz-client-key"
}

resource "aws_secretsmanager_secret_version" "authz_client_key" {
  secret_id     = aws_secretsmanager_secret.authz_client_key.id
  secret_string = tls_private_key.authz_client.private_key_pem
}

module "ecs_service" {
  source = "../../modules/ecs_service"

  name_prefix                = local.name_prefix
  environment                = "dev"
  aws_region                 = var.aws_region
  vpc_id                     = data.aws_vpc.foundation.id
  private_subnet_ids         = data.aws_subnets.foundation_private.ids
  vpc_link_security_group_id = data.aws_security_group.api_gateway_vpc_link.id
  log_group_name             = "/ai-platform/ecs/bedrock-gateway-dev"
  # Plan section 35.5 -- same shared internal Private CA
  # platform-authz-service's own ALB HTTPS listener uses (local.private_ca_arn
  # above, also used by this file's own mTLS client-cert resources below).
  private_ca_arn = local.private_ca_arn
  sns_topic_arn  = aws_sns_topic.ops_alerts.arn

  # No image has been pushed on a first apply -- CI registers the real
  # task definition revision on its first deploy (see infra/README.md).
  # The service will show 0 running tasks until then; expected.
  image = "${module.ecr.repository_url}:bootstrap"

  desired_count          = var.desired_count
  task_cpu               = var.task_cpu
  task_memory            = var.task_memory
  enable_execute_command = true

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
    ENVIRONMENT                  = "dev"
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

    ONBOARDING_REQUESTS_TABLE_NAME            = aws_dynamodb_table.onboarding_requests.name
    ONBOARDING_AUDIT_TABLE_NAME               = aws_dynamodb_table.onboarding_audit.name
    PROVISIONED_TENANT_POLICIES_TABLE_NAME    = aws_dynamodb_table.provisioned_tenant_policies.name
    PROVISIONED_PRINCIPAL_MAPPINGS_TABLE_NAME = aws_dynamodb_table.provisioned_principal_mappings.name

    POLICY_CHANGE_REQUESTS_TABLE_NAME              = aws_dynamodb_table.policy_change_requests.name
    PROVISIONED_TENANT_POLICIES_HISTORY_TABLE_NAME = aws_dynamodb_table.provisioned_tenant_policies_history.name

    # M12 (plan.md Section 5): delegates AWS_IAM principal mapping to
    # authz-service instead of resolving it in-process. HTTPS via a
    # private-CA-issued cert (see platform-foundation's
    # aws_acmpca_certificate_authority.internal) -- this is an internal
    # call between two ECS tasks
    # in the same VPC (platform-authz-service's own ALB is
    # `internal = true` and its security group only accepts traffic
    # from this service's own task SG, looked up above), but encrypted
    # in transit and authenticated against the pinned CA rather than
    # relying on the VPC boundary alone.
    AUTHZ_SERVICE_URL = "https://${data.aws_lb.authz.dns_name}"
    # HttpIamTenantResolver trusts exactly this CA (not the system
    # trust store) since it's privately issued -- see
    # auth/aws_iam.py's HttpIamTenantResolver and config.py.
    #
    # Hardcoded, not a resource reference: the CA moved to
    # platform-foundation (Phase 3c, 2026-09-21). A root CA's
    # self-signed certificate doesn't change for the CA's lifetime, so
    # this is stable -- update this (fetch fresh via `aws acm-pca
    # get-certificate-authority-certificate`) if the CA is ever
    # destroyed and recreated.
    AUTHZ_CA_CERT_PEM = chomp(<<-EOT
      -----BEGIN CERTIFICATE-----
      MIIDKzCCAhOgAwIBAgIRAJI2amN73dXcl32YjkCVK5swDQYJKoZIhvcNAQELBQAw
      LzEtMCsGA1UEAwwkQmVkcm9jayBHYXRld2F5IFBsYXRmb3JtIEludGVybmFsIENB
      MB4XDTI2MDkxNzAxNDMyNVoXDTM2MDkxNzAyNDMyNVowLzEtMCsGA1UEAwwkQmVk
      cm9jayBHYXRld2F5IFBsYXRmb3JtIEludGVybmFsIENBMIIBIjANBgkqhkiG9w0B
      AQEFAAOCAQ8AMIIBCgKCAQEAop+Y1RxTXoOTZVrIgFurEINkEbE/E1/JQjHesTMX
      2zuFmgQAJtmsLRHnEpJDPRSWjcbdZCVbhSsfNGt7gNXIw32pPTbPOx02BoHUVaFS
      MbNaw6t0TRvsuWTCrJCTRIoS595xrUSz1jFuwIMgpzJH7C0u6OoMEI+YrU6WYOhX
      pKsT5AQrVf7e6BaRX4IeyOZRK8A7ACq0NqrgVDv+gmq8ggnAWZyMBsSscozkOfZO
      FK+fnsK2xdSiDvvoBOiN2wC3zx6ZyTqzN0zsAaqq9hQby3y2GD/FDyIq4MIYSsqR
      YlhWJp5HL+BVnJ66sn9MqnKbNUEM3EI71DVX5wzGzvpCUwIDAQABo0IwQDAPBgNV
      HRMBAf8EBTADAQH/MB0GA1UdDgQWBBREKqAldQXFXQVILzNgPFMgmKAHMDAOBgNV
      HQ8BAf8EBAMCAYYwDQYJKoZIhvcNAQELBQADggEBAHnic2MRaOxmzBWU4/A1hYmq
      tdipEjk2BXt3uOUOkbiPn3lYneZCcQIUfSrDP65d+3+5aTPV2oVGU93zc+YrUwjN
      QSQWYP0QrXWBa2ZOAou354Jg5je1ydVRZi2QdnIuIEkHdbkY10zAy8b4ojc75tDE
      vmJoVAJhXQnjiLl0NeR0rPY4cTdPKnZ+Wphb2cl8hEGzYr6s7TMQvbPjzB0HrnFX
      OTDWYTh2wY7wKxcWzp1rlgul/jH1Kek4eBtG3u3F/2R8MBxYfI5XzOyoWayzXVd5
      LrdHmzHQfP2eEv9GqS54Gqu3elV3dOdluK0rbmYfrUcVGOoFI3DFBFLD01agWe0=
      -----END CERTIFICATE-----
    EOT
    )

    # Tracing: the ADOT sidecar (modules/ecs_service) listens on
    # localhost within this same task (awsvpc mode -- one network
    # namespace per task), exporting to X-Ray. Full /v1/traces path
    # required: telemetry/otel.py passes this straight to
    # OTLPSpanExporter(endpoint=...), which only auto-appends that path
    # when reading the endpoint from an env var ITSELF, not when it's
    # passed explicitly like this.
    OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318/v1/traces"

    # Human auth (Cognito, via the portal) -- separate from the
    # AWS_IAM/SigV4 path service/application callers already use
    # (auth/aws_iam.py), which needs nothing here. Falls back to the
    # dev JWT keypair (config.py) if OIDC_JWKS_URL is ever unset.
    #
    # Hardcoded, not a module reference: cognito_idp moved to the
    # platform-control-plane repo (Phase 1 of the platform
    # restructuring -- portal/cognito infra ownership) -- these are
    # that user pool/client's real, already-live values (confirmed via
    # `terraform state show` before the migration). A clean data-source
    # lookup here would need aws_cognito_user_pool_clients + an
    # index()/element() match on client name, more fragile than just
    # hardcoding a value this stable -- same reasoning as this file's
    # own (now-removed) portal_base_url hardcode. Update these if the
    # user pool or app client is ever destroyed and recreated (see
    # platform-control-plane/infra's outputs.tf for the current real
    # values).
    OIDC_JWKS_URL = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_HvCI4Nbr6/.well-known/jwks.json"
    OIDC_ISSUER   = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_HvCI4Nbr6"
    OIDC_AUDIENCE = "3g1ahkm9un6ccfno3e2jtt53j8"
  }

  # mTLS cutover (plan section 35): the client cert/key gateway-api
  # presents to authz-service's ALB -- resolved from Secrets Manager at
  # container start, injected as a real env var HttpIamTenantResolver
  # reads the same way it already reads AUTHZ_CA_CERT_PEM above, never
  # persisted in the task definition or CloudWatch Logs.
  container_secrets = {
    AUTHZ_CLIENT_CERT_PEM = aws_secretsmanager_secret.authz_client_cert.arn
    AUTHZ_CLIENT_KEY_PEM  = aws_secretsmanager_secret.authz_client_key.arn
  }
}

# --- M7: async jobs -----------------------------------------------------

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${local.name_prefix}-jobs-dlq"
  message_retention_seconds = 1209600 # 14 days

  tags = {
    Environment = "dev"
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
    Environment = "dev"
  }
}

resource "aws_dynamodb_table" "jobs" {
  # Renamed 2026-09-23 from gateway-dev-jobs to the <name>-<env> convention
  # -- see aws_dynamodb_table.provisioned_principal_mappings's comment for
  # why (ForceNew name, so this was create-new/copy-items/cut-over/
  # decommission-old, not a destroy+recreate); state re-pointed via
  # `state rm` + `import`.
  name         = "gateway-jobs-dev"
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
    Environment = "dev"
  }
}

# Plan section 35.12 (P2 production hardening): customer-managed KMS
# key for both audit buckets (below) -- shared, not one key per
# bucket, since both are the same "audit tier" data and a second key
# would just double the $1/mo base cost for no isolation benefit
# neither bucket needs from the other. The real value over SSE-S3's
# AES256 (what both buckets used before this) isn't the encryption
# itself -- it's that every encrypt/decrypt call against this key
# shows up in CloudTrail attributed to a specific IAM principal, which
# AES256's opaque S3-managed keys never produce. Rotation enabled
# (AWS rotates the underlying key material yearly; the CMK's own ARN/
# grants never change, so this is transparent to every caller).
resource "aws_kms_key" "audit" {
  description         = "${local.name_prefix} audit buckets (S3AuditStore, S3RequestAuditStore)"
  enable_key_rotation = true

  tags = {
    Environment = "dev"
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
    Environment = "dev"
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
# audit bucket above: metadata-only (no prompt/response text), always
# on (not opt-in), Object Lock enabled for real write-once-read-many
# durability. GOVERNANCE mode + a short 90-day default here
# deliberately, not COMPLIANCE -- COMPLIANCE mode means literally
# nobody (including account root) can delete an object before
# retention expires, and Object Lock can only be set at bucket
# creation, never retrofitted. Setting that here, on a bucket CI
# auto-applies to dev the moment this merges, would create a
# financially-stuck bucket the first time any test writes to it. A
# real prod rollout should choose COMPLIANCE + a genuine regulatory
# retention (7 years is typical for insurance) as its own deliberate
# decision at prod-apply time -- see environments/prod/main.tf's own
# comment on this same resource for why it stays unapplied.
resource "aws_s3_bucket" "request_audit" {
  bucket              = "${local.name_prefix}-audit-immutable"
  object_lock_enabled = true

  tags = {
    Environment = "dev"
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
      mode = "GOVERNANCE"
      days = 90
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
  # provider bug live ("Provider returned invalid result object after
  # apply ... unknown value for ... description"), leaving the
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
      output_strength = "NONE" # PROMPT_ATTACK is an input-only concept -- AWS requires output_strength set, NONE is the correct value here, not a weaker choice
    }
  }

  tags = {
    Environment = "dev"
  }
}

# Plan section 35's P1 hardening: a published, IMMUTABLE numbered
# version instead of DRAFT -- a Bedrock guardrail version is a
# point-in-time snapshot, not a live pointer, so this resource never
# updates itself when aws_bedrock_guardrail.this's own config changes
# later. To publish a NEW guardrail version, add a NEW
# aws_bedrock_guardrail_version resource (e.g. "v2") alongside this
# one and repoint BEDROCK_GUARDRAIL_VERSION at it below -- don't
# expect editing this resource in place to do anything (Bedrock has no
# "update a published version" operation; the whole point is that it
# can't change out from under a caller relying on it).
# skip_destroy = true -- a `terraform destroy` must not delete a
# published version out from under whatever's still configured to use
# its exact version number.
resource "aws_bedrock_guardrail_version" "v1" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Initial published version -- SSN/card/email PII + prompt-attack filtering."
  skip_destroy  = true
}

# --- M8: FinOps -----------------------------------------------------------

# One row per (tenant_id, month) -- a new month is just a new row, no
# reset job needed. month is "YYYY-MM" UTC (see usage/store.py).
resource "aws_dynamodb_table" "usage" {
  # Renamed 2026-09-23 from gateway-dev-usage -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment.
  name         = "gateway-usage-dev"
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
    Environment = "dev"
  }
}

# --- Plan section 35.2: distributed admission control (P0 production
# hardening) -- backs both DynamoDbConcurrencyLimiter and
# DynamoDbRateLimiter (services/gateway/concurrency.py,
# services/gateway/policy/rate_limiter.py). One table for both,
# distinguished by `pk` prefix ("concurrency#..." vs "ratelimit#...")
# -- no reason to provision two tables for what's structurally the
# same "one row per counter/bucket, hash-keyed, PAY_PER_REQUEST" shape.
#
# Deliberately no point_in_time_recovery (plan section 35's P2
# hardening adds PITR to every OTHER table below) -- this one holds
# only ephemeral, self-healing runtime counters (in-flight request
# counts, rate-limit token buckets), never data worth restoring; a
# point-in-time restore would just reintroduce stale counter values.
resource "aws_dynamodb_table" "admission_control" {
  # Renamed 2026-09-23 from gateway-dev-admission-control -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment for the
  # rename procedure; unlike that table's data, this one's counters were
  # deliberately left to start fresh on the new table (see this block's
  # own comment above on why PITR/restoring old counter values here was
  # never meaningful anyway).
  name         = "gateway-admission-control-dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Plan section 35.18 (P0 production hardening): backstop storage
  # cleanup for DynamoDbConcurrencyLimiter's per-request lease items
  # (concurrency.py) -- native TTL deletion doesn't run any of this
  # app's code, so it does NOT compensate the global/tenant counters
  # itself (that's DynamoDbConcurrencyLimiter.reconcile()'s job,
  # triggered probabilistically from try_acquire); this only stops
  # stale lease items from accumulating in storage forever if
  # reconcile() were somehow never called. Free -- DynamoDB TTL has no
  # separate cost. No effect on the rate-limit bucket items this same
  # table also holds (they don't set this attribute).
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Environment = "dev"
  }
}

# --- M11: Application Onboarding (plan section 22) -------------------------

resource "aws_dynamodb_table" "onboarding_requests" {
  # Renamed 2026-09-23 from gateway-dev-onboarding-requests -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment.
  name         = "gateway-onboarding-requests-dev"
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
    Environment = "dev"
  }
}

# One row per (request_id, timestamp) -- a request's audit history is
# always read as one ordered sequence, never looked up by event alone
# (see onboarding/audit.py).
resource "aws_dynamodb_table" "onboarding_audit" {
  # Renamed 2026-09-23 from gateway-dev-onboarding-audit -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment.
  name         = "gateway-onboarding-audit-dev"
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
    Environment = "dev"
  }
}

# Provisioned-only overlay on top of policies/tenants.yaml -- a tenant
# provisioned through onboarding lives here; an existing hand-managed
# tenant never does (see policy/store.py's LayeredPolicyStore).
resource "aws_dynamodb_table" "provisioned_tenant_policies" {
  # Renamed 2026-09-23 from gateway-dev-provisioned-tenant-policies to
  # gateway-tenant-policies-dev -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment.
  name         = "gateway-tenant-policies-dev"
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
    Environment = "dev"
  }
}

# Provisioned-only overlay on top of policies/iam_tenants.yaml -- same
# reasoning as provisioned_tenant_policies above, for the AWS_IAM/SigV4
# auth path (see auth/aws_iam.py's LayeredIamTenantResolver).
resource "aws_dynamodb_table" "provisioned_principal_mappings" {
  # Renamed 2026-09-22 from gateway-dev-provisioned-principal-mappings --
  # deliberately not local.name_prefix-derived like every sibling table
  # here, per this rename's explicit request. Data migrated via a
  # create-new/copy-items/cut-over/decommission-old sequence (never a
  # destroy+recreate -- DynamoDB's `name` is ForceNew, which would have
  # dropped all 12 live onboarded grants); this resource's Terraform
  # state was re-pointed at the new table via `state rm` + `import`,
  # never applied as a replace.
  name         = "gateway-principal-grants-dev"
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
    Environment = "dev"
  }
}

# --- Plan section 33: policy versioning/approval/rollback -----------------

# One row per proposed edit to an already-provisioned tenant's policy --
# see services/gateway/policy/change_requests.py's PolicyChangeRequest.
resource "aws_dynamodb_table" "policy_change_requests" {
  # Renamed 2026-09-23 from gateway-dev-policy-change-requests -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment.
  name         = "gateway-policy-change-requests-dev"
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
    Environment = "dev"
  }
}

# Every past version of a provisioned tenant's policy, keyed by the
# policy_epoch it was current at -- what apply_change()/set_state()
# archive before overwriting, and what rollback() reads back from (see
# services/gateway/policy/store.py's DynamoDbPolicyStore._archive()).
resource "aws_dynamodb_table" "provisioned_tenant_policies_history" {
  # Renamed 2026-09-23 from gateway-dev-provisioned-tenant-policies-history
  # to gateway-tenant-policies-history-dev -- see
  # aws_dynamodb_table.provisioned_principal_mappings's comment.
  name         = "gateway-tenant-policies-history-dev"
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
    Environment = "dev"
  }
}

# --- M12: Authorization Service (plan.md Section 5) -------------------
#
# Plan section 36: authz_service's own ECS/ALB/ECR Terraform moved to
# platform-authz-service -- see its environments/dev/main.tf. The
# genuinely shared resources this repo used to alone-own here (the
# private CA authz-service's ALB cert is issued from -- but so could
# any other internal service's be) moved to platform-foundation in
# turn (Phase 3c, 2026-09-21, see that repo's environments/dev/main.tf).
# What's left here: the ops_alerts SNS topic, and the
# provisioned_principal_mappings table (written by this repo's own
# onboarding flow -- see platform-control-plane once THAT migration
# lands -- read by authz-service).

module "worker_service" {
  source = "../../modules/worker_service"

  name_prefix        = "${local.name_prefix}-worker"
  environment        = "dev"
  aws_region         = var.aws_region
  vpc_id             = data.aws_vpc.foundation.id
  private_subnet_ids = data.aws_subnets.foundation_private.ids
  cluster_name       = module.ecs_service.cluster_name
  log_group_name     = "/ai-platform/ecs/bedrock-gateway-worker-dev"

  # Same image as gateway-api -- same codebase, different command. CI's
  # deploy-dev job updates this task definition alongside gateway-api's
  # on every push, both pointing at the one image it just built.
  image   = "${module.ecr.repository_url}:bootstrap"
  command = ["python", "-m", "services.worker.main"]

  desired_count = 1
  task_cpu      = var.task_cpu
  task_memory   = var.task_memory

  bedrock_model_ids  = var.bedrock_model_ids
  sqs_queue_arn      = aws_sqs_queue.jobs.arn
  sqs_queue_name     = aws_sqs_queue.jobs.name
  sqs_dlq_name       = aws_sqs_queue.jobs_dlq.name
  sns_topic_arn      = aws_sns_topic.ops_alerts.arn
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
    ENVIRONMENT                            = "dev"
    LOG_LEVEL                              = "INFO"
    ROUTE_SET_CONFIG_PATH                  = "policies/route_sets.yaml"
    TENANT_POLICY_PATH                     = "policies/tenants.yaml"
    IAM_TENANTS_PATH                       = "policies/iam_tenants.yaml"
    JOBS_QUEUE_URL                         = aws_sqs_queue.jobs.url
    JOBS_TABLE_NAME                        = aws_dynamodb_table.jobs.name
    USAGE_TABLE_NAME                       = aws_dynamodb_table.usage.name
  }
}

