# ECS Fargate service fronted by a private ALB, reachable only through
# API Gateway's VPC Link (see the platform-edge-gateway repo, split out
# of this one -- plan.md Section 25) -- never directly from the
# internet. Plan section 35.5: an HTTPS listener now exists alongside
# the original HTTP one (see aws_lb_listener.https/https_listener_port
# below) -- platform-edge-gateway's VPC Link integration still points
# at the HTTP one today; cutting it over to 443 is a deliberate,
# separately-verified follow-up, not done automatically by this
# listener merely existing.

resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = var.log_group_name != "" ? var.log_group_name : "/ecs/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

# --- Security groups -------------------------------------------------

resource "aws_security_group" "alb" {
  name = "${var.name_prefix}-alb"
  # NOTE: description is immutable on an existing security group (AWS
  # ForceNew) -- changing this string forces Terraform to replace the
  # whole SG, which then fails with DependencyViolation as long as
  # anything else (the ECS "service" SG's ingress rule, the ALB itself)
  # still references its id. Left as the original text on purpose; the
  # actual behavior change (VPC-Link-only ingress, below) doesn't need
  # a description change to take effect.
  description = "ALB ingress from the internet on ${var.listener_port}"
  vpc_id      = var.vpc_id

  ingress {
    description     = "From the API Gateway VPC Link"
    from_port       = var.listener_port
    to_port         = var.listener_port
    protocol        = "tcp"
    security_groups = [var.vpc_link_security_group_id]
  }

  # Plan section 35.5 -- the HTTPS listener below, alongside the HTTP
  # one above (not a replacement yet, see https_listener_port's own
  # comment). Only opened when private_ca_arn is actually set (dev
  # today; prod has no private CA of its own yet) -- an always-open
  # 443 with nothing listening on it would be dead attack surface for
  # no benefit.
  dynamic "ingress" {
    for_each = var.private_ca_arn != null ? [1] : []
    content {
      description     = "From the API Gateway VPC Link, HTTPS"
      from_port       = var.https_listener_port
      to_port         = var.https_listener_port
      protocol        = "tcp"
      security_groups = [var.vpc_link_security_group_id]
    }
  }

  # Added by hand 2026-09-22 for the gateway-principal-grants-dev
  # DynamoDB rename's manual verification -- lets a console-launched
  # AWS CloudShell VPC environment (attached to gateway-dev-vpc,
  # security group sg-0c25b89ac47694f02) reach this ALB for ad hoc
  # curl/debugging. Declared here (not just created via the CLI) so a
  # routine dev auto-apply doesn't revert it as drift.
  ingress {
    description     = "CloudShell VPC environment - manual curl/debugging"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = ["sg-0c25b89ac47694f02"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_security_group" "service" {
  name        = "${var.name_prefix}-service"
  description = "Gateway task ingress from the ALB only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "From ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Environment = var.environment
  }
}

# --- Load balancer -----------------------------------------------------

resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.private_subnet_ids

  tags = {
    Environment = var.environment
  }
}

resource "aws_lb_target_group" "this" {
  name        = "${var.name_prefix}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = var.listener_port
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

# Plan section 35.5 -- same private-CA cert pattern
# modules/authz_service already uses for its own ALB. Short
# placeholder CN (ACM's 64-char CN limit -- this ALB's real DNS name
# is longer), real hostname verification happens via
# subject_alternative_names. count-gated on private_ca_arn (see that
# variable's own comment) rather than required, since prod has no
# private CA of its own yet.
resource "aws_acm_certificate" "this" {
  count                     = var.private_ca_arn != null ? 1 : 0
  domain_name               = "gateway.internal"
  subject_alternative_names = [aws_lb.this.dns_name]
  certificate_authority_arn = var.private_ca_arn
}

resource "aws_lb_listener" "https" {
  count             = var.private_ca_arn != null ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = var.https_listener_port
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.this[0].arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

# --- IAM ---------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Pulls the image from ECR and writes to CloudWatch -- standard ECS
# execution role, no application permissions.
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The execution role (not the task role) is what actually resolves
# container_secrets at container start -- ECS Agent assumes this role
# to call secretsmanager:GetSecretValue, before the app itself ever
# runs. Scoped to exactly the ARNs this call site passed in, not a
# broad secretsmanager:* grant.
data "aws_iam_policy_document" "execution_secrets" {
  count = length(var.container_secrets) > 0 ? 1 : 0

  statement {
    sid     = "ReadContainerSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    # A container_secrets value may be a full ARN, or ARN:jsonKey for
    # one field of a JSON secret -- either way, IAM authorizes against
    # the base secret ARN, so always take just the first 7 ":"-separated
    # segments (arn:aws:secretsmanager:region:account:secret:name).
    resources = distinct([for arn in values(var.container_secrets) : join(":", slice(split(":", arn), 0, 7))])
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  count  = length(var.container_secrets) > 0 ? 1 : 0
  name   = "${var.name_prefix}-execution-secrets-read"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets[0].json
}

# What the running application is allowed to do: call Bedrock. Nothing else.
resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

locals {
  # "us."-prefixed model IDs are cross-region inference profiles (see
  # policies/route_sets.yaml) -- Bedrock authorizes both the profile
  # ARN itself and the underlying foundation-model ARN in every region
  # the profile can route to. Non-"us."-prefixed IDs are used as plain
  # foundation-model ARNs in var.aws_region.
  us_profile_ids   = [for id in var.bedrock_model_ids : id if startswith(id, "us.")]
  direct_model_ids = [for id in var.bedrock_model_ids : id if !startswith(id, "us.")]

  inference_profile_arns = [
    for id in local.us_profile_ids :
    "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${id}"
  ]
  underlying_model_arns = flatten([
    for id in local.us_profile_ids : [
      for region in var.bedrock_profile_regions :
      "arn:aws:bedrock:${region}::foundation-model/${trimprefix(id, "us.")}"
    ]
  ])
  direct_model_arns = [
    for id in local.direct_model_ids :
    "arn:aws:bedrock:${var.aws_region}::foundation-model/${id}"
  ]

  bedrock_model_arns = concat(local.inference_profile_arns, local.underlying_model_arns, local.direct_model_arns)
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid = "InvokeBedrockModels"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_model_arns
  }

  # BedrockGuardrailClient (services/gateway/guardrails/bedrock_guardrail.py)
  statement {
    sid       = "ApplyGuardrail"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [var.bedrock_guardrail_arn]
  }
}

resource "aws_iam_role_policy" "task_bedrock" {
  name   = "${var.name_prefix}-bedrock-invoke"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}

# ECS Exec (`aws ecs execute-command`) needs the task to be able to open
# an SSM Session Manager channel -- debugging/curl-testing convenience,
# off by default (var.enable_execute_command).
data "aws_iam_policy_document" "ecs_exec" {
  count = var.enable_execute_command ? 1 : 0

  statement {
    sid = "EcsExec"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_ecs_exec" {
  count  = var.enable_execute_command ? 1 : 0
  name   = "${var.name_prefix}-ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.ecs_exec[0].json
}

# Enqueue side of M7's async jobs feature (see modules/worker_service
# for the consumer side). Unconditional rather than count-gated on
# nullability: a count that depends on a not-yet-created resource's ARN
# (aws_sqs_queue.jobs.arn, on this module's very first apply alongside
# that queue) is "known after apply", and Terraform refuses to plan a
# count from a value it can't resolve yet ("Invalid count argument").
# Every current caller of this module already has a jobs queue/table,
# so there's no real optionality being given up here.
data "aws_iam_policy_document" "jobs_access" {
  statement {
    sid       = "EnqueueJobs"
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = [var.jobs_queue_arn]
  }

  statement {
    sid       = "JobRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [var.jobs_table_arn]
  }

  statement {
    sid       = "UsageRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [var.usage_table_arn]
  }

  # Plan section 35.2: DynamoDbConcurrencyLimiter uses TransactWriteItems
  # with Update/Put/Delete actions (dynamodb:UpdateItem/PutItem/DeleteItem
  # is what's actually checked for those, not a separate
  # TransactWriteItems action -- AWS authorizes a transaction by the
  # underlying per-item operation); DynamoDbRateLimiter uses a plain
  # conditional PutItem/GetItem CAS.
  #
  # Plan section 35.18: the TTL-lease redesign added a Delete transact
  # item (release()'s own lease cleanup, and reconcile()'s compensation
  # of an expired lease) and reconcile()'s own dynamodb:Scan (sweeping
  # stale lease items) -- live-caught: a real request 500'd with
  # AccessDeniedException on dynamodb:DeleteItem, since moto's tests
  # don't enforce real IAM and this grant was never added alongside
  # that code change.
  statement {
    sid = "AdmissionControl"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Scan",
    ]
    resources = [var.admission_control_table_arn]
  }
}

resource "aws_iam_role_policy" "task_jobs" {
  name   = "${var.name_prefix}-jobs-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.jobs_access.json
}

# M11 Application Onboarding (plan section 22) -- provisioning runs
# inline on the admin approve call (no separate worker), so gateway-api
# itself needs read/write on all four tables. Scan is genuinely needed,
# not a broad-grant shortcut: list_all()/list_tenant_ids()/list_grants()
# have no better-known key to Query by at this scale (same tradeoff
# jobs/store.py's DynamoDbJobStore already accepts).
data "aws_iam_policy_document" "onboarding_access" {
  statement {
    sid       = "OnboardingRequests"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan"]
    resources = [var.onboarding_requests_table_arn]
  }

  statement {
    sid       = "OnboardingAudit"
    actions   = ["dynamodb:PutItem", "dynamodb:Query"]
    resources = [var.onboarding_audit_table_arn]
  }

  statement {
    sid       = "ProvisionedTenantPolicies"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan"]
    resources = [var.provisioned_tenant_policies_table_arn]
  }

  statement {
    sid       = "ProvisionedPrincipalMappings"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan"]
    resources = [var.provisioned_principal_mappings_table_arn]
  }
}

resource "aws_iam_role_policy" "task_onboarding" {
  name   = "${var.name_prefix}-onboarding-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.onboarding_access.json
}

# Plan section 33: policy versioning/approval/rollback. DynamoDbPolicyStore's
# apply_change() does a conditional PutItem (dynamodb:PutItem covers it --
# ConditionExpression isn't a separate IAM action); rollback() does a
# GetItem on the history table by (tenant_id, policy_epoch); list_history()
# Queries it. PolicyChangeStore's list_for_tenant() Scans, same tradeoff as
# onboarding_access above.
data "aws_iam_policy_document" "policy_versioning_access" {
  statement {
    sid       = "PolicyChangeRequests"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan"]
    resources = [var.policy_change_requests_table_arn]
  }

  statement {
    sid       = "ProvisionedTenantPoliciesHistory"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [var.provisioned_tenant_policies_history_table_arn]
  }
}

resource "aws_iam_role_policy" "task_policy_versioning" {
  name   = "${var.name_prefix}-policy-versioning-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.policy_versioning_access.json
}

# routing/model_quota.py's ModelQuotaCache -- "quota#<model_id>" rows,
# config written only by scripts/sync_model_quotas_from_aws.py running
# out-of-band. GetItem-only: gateway-api only ever reads this table.
#
# ModelQuotaLimiter's own live rate-limit counter rows
# ("ratelimit#model#<model_id>" and its _tenant/_tpm variants) live in
# a SEPARATE table (model_ratelimits_table_arn) -- needs the same
# actions its tenant-scoped sibling already has on
# admission_control_table_arn above (GetItem for the read-before-CAS,
# PutItem for the CAS write itself).
data "aws_iam_policy_document" "model_quota_access" {
  statement {
    sid       = "ModelQuotaConfig"
    actions   = ["dynamodb:GetItem"]
    resources = [var.model_quotas_table_arn]
  }
  statement {
    sid       = "ModelRateLimitCounters"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [var.model_ratelimits_table_arn]
  }
}

resource "aws_iam_role_policy" "task_model_quota" {
  name   = "${var.name_prefix}-model-quota-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.model_quota_access.json
}

# S3AuditStore (services/gateway/telemetry/debug_capture.py) -- write
# only, no Get/List/Delete. This is a durable audit trail; the gateway
# task itself has no business reading its own past writes back, let
# alone deleting them.
data "aws_iam_policy_document" "audit_access" {
  statement {
    sid       = "AuditPayloadWrite"
    actions   = ["s3:PutObject"]
    resources = ["${var.audit_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "task_audit" {
  name   = "${var.name_prefix}-audit-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.audit_access.json
}

# S3RequestAuditStore (services/gateway/telemetry/request_audit.py) --
# same write-only reasoning as audit_access above. Object Lock is
# enforced at the bucket, not here -- IAM PutObject on a locked bucket
# still works, the lock only prevents deletion before retention
# expires.
data "aws_iam_policy_document" "request_audit_access" {
  statement {
    sid       = "RequestAuditWrite"
    actions   = ["s3:PutObject"]
    resources = ["${var.request_audit_bucket_arn}/*"]
  }
}

# Plan section 35.12: both audit buckets' shared SSE-KMS key. Both
# task_audit/task_request_audit only ever PUT (never GET) their own
# audit objects, so GenerateDataKey* is the only action actually
# needed -- Decrypt isn't, and isn't granted (this task has no reason
# to read its own past writes back, same reasoning S3AuditStore's own
# docstring already gives for why it's write-only in S3 terms too).
data "aws_iam_policy_document" "audit_kms_access" {
  statement {
    sid       = "AuditKmsGenerateDataKey"
    actions   = ["kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext"]
    resources = [var.audit_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "task_audit_kms" {
  name   = "${var.name_prefix}-audit-kms-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.audit_kms_access.json
}

resource "aws_iam_role_policy" "task_request_audit" {
  name   = "${var.name_prefix}-request-audit-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.request_audit_access.json
}

# --- Tracing: ADOT sidecar -> X-Ray -------------------------------------
#
# Own config passed inline via AOT_CONFIG_CONTENT (the ADOT image's own
# documented mechanism for this) rather than the image's bundled ECS
# preset -- explicit and known-correct beats guessing the preset's
# exact filename/behavior. Reachable at localhost:4318 from the app
# container since ECS tasks share one network namespace (awsvpc mode).
locals {
  adot_collector_config = <<-EOT
    receivers:
      otlp:
        protocols:
          http:
            endpoint: 0.0.0.0:4318
    exporters:
      awsxray:
        region: ${var.aws_region}
    service:
      pipelines:
        traces:
          receivers: [otlp]
          exporters: [awsxray]
  EOT
}

data "aws_iam_policy_document" "xray_write" {
  statement {
    sid       = "XRayWrite"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_xray" {
  name   = "${var.name_prefix}-xray-write"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.xray_write.json
}

# --- Task definition + service ------------------------------------------

resource "aws_ecs_task_definition" "this" {
  family                   = var.name_prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "gateway-api"
      image     = var.image
      essential = true
      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]
      environment = [
        for k, v in var.container_env : { name = k, value = v }
      ]
      secrets = [
        for k, v in var.container_secrets : { name = k, valueFrom = v }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "gateway"
        }
      }
    },
    {
      # essential = false: a sidecar problem shouldn't take the whole
      # task down -- tracing is additive observability, not a hard
      # dependency for serving traffic.
      name      = "aws-otel-collector"
      image     = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
      essential = false
      environment = [
        { name = "AOT_CONFIG_CONTENT", value = local.adot_collector_config }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "adot"
        }
      }
    }
  ])

  tags = {
    Environment = var.environment
  }
}

resource "aws_ecs_service" "this" {
  name            = "${var.name_prefix}-service"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  enable_execute_command = var.enable_execute_command

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "gateway-api"
    container_port   = var.container_port
  }

  # CI updates the running image with `aws ecs update-service
  # --force-new-deployment` against a new task definition revision it
  # registers itself; ignore drift here so `terraform apply` doesn't
  # fight deploys made outside of Terraform.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]

  tags = {
    Environment = var.environment
  }
}

# --- Autoscaling (plan section 35.6, P0 production hardening) ------------
#
# desired_count is already in the service's own lifecycle.ignore_changes
# above (CI-deploy-owned) -- autoscaling's own changes to it need the
# same treatment, which App Autoscaling gets for free by managing
# desired_count out-of-band via the scalable target below, not through
# this resource's own argument.

resource "aws_appautoscaling_target" "this" {
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = var.autoscaling_min_capacity
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name_prefix}-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  service_namespace  = aws_appautoscaling_target.this.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 60
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

# Request-count scaling on top of CPU -- this is an I/O-bound gateway
# (waiting on Bedrock/guardrail calls, not CPU-bound), so ALB request
# volume per task is often the earlier, more accurate scale-out signal;
# both policies are active simultaneously (App Autoscaling scales out on
# whichever policy asks for more capacity, scales in only when every
# policy agrees it's safe to).
resource "aws_appautoscaling_policy" "requests" {
  name               = "${var.name_prefix}-request-count-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  service_namespace  = aws_appautoscaling_target.this.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.this.arn_suffix}/${aws_lb_target_group.this.arn_suffix}"
    }
    target_value       = 200
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

# --- Operational alarms (plan section 35's P1 hardening) -----------------
#
# Three signals chosen as the highest-value minimum: 5xx rate (the
# gateway itself failing, not a client error), p95 latency (the same
# SLO framing this platform's own admission-control research targets
# -- plan section 31), and unhealthy target count (the ALB has nothing
# left to route to, the most severe of the three). Not an exhaustive
# alarm catalog -- guardrail-failure/authz-unavailable/budget-anomaly
# alarms live closer to the signals they're about (see
# modules/worker_service's DLQ alarm, and plan section 34.7's
# /v1/admin/usage/anomalies for the budget signal this doesn't yet
# have an alarm wired to).

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name          = "${var.name_prefix}-target-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Gateway is returning 5xx to its own ALB -- an application failure, not a client error (4xx)."
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
  }
  alarm_actions = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
  ok_actions    = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
}

resource "aws_cloudwatch_metric_alarm" "p95_latency" {
  alarm_name          = "${var.name_prefix}-p95-latency"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "TargetResponseTime"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  extended_statistic  = "p95"
  threshold           = 3 # seconds -- same order of magnitude as TenantSlo.p95_latency_ms defaults (policy/models.py)
  alarm_description   = "Gateway p95 latency (ALB-measured, includes Bedrock call time) is elevated for 3 consecutive minutes."
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
  }
  alarm_actions = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
  ok_actions    = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${var.name_prefix}-unhealthy-hosts"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  alarm_description   = "At least one gateway task is failing its ALB health check."
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
    TargetGroup  = aws_lb_target_group.this.arn_suffix
  }
  alarm_actions = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
  ok_actions    = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
}
