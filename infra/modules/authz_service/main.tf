# ECS Fargate authz-service (M12, plan.md Section 5) fronted by a
# private ALB reachable only from gateway-api's own task security
# group -- never API Gateway, never the internet. Purely an internal
# service call (Bedrock Gateway -> Authorization Service), not a new
# public surface.

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

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "authz-service ALB -- ingress from the gateway-api task SG only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "From gateway-api, HTTPS only"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.caller_security_group_id]
  }

  # Phase 4 (2026-09-21, "direct cutover"): platform-control-plane's
  # backend_service also calls authz-service directly now (same
  # mTLS-verified path as gateway-api). Optional/nullable so
  # environments without that backend live yet (prod today) don't
  # need to pass it.
  dynamic "ingress" {
    for_each = var.control_plane_caller_security_group_id != null ? [var.control_plane_caller_security_group_id] : []
    content {
      description     = "From control-plane backend, HTTPS only"
      from_port       = 443
      to_port         = 443
      protocol        = "tcp"
      security_groups = [ingress.value]
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

  ingress {
    description     = "CloudShell VPC environment - manual curl/debugging (HTTPS)"
    from_port       = 443
    to_port         = 443
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
  description = "authz-service task ingress from its own ALB only"
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

# Private-CA-issued -- no domain ownership validation needed (unlike a
# public ACM cert), so the ALB's own auto-generated DNS name can be
# used directly, no custom Route53 zone required. It can't be the
# domain_name/CN field itself though -- ACM enforces a 64-character CN
# limit (learned live: "internal-gateway-dev-authz-alb-..." is 69) --
# so a short placeholder goes there instead, and the real (long) ALB
# DNS name goes in subject_alternative_names, which is what TLS
# clients actually verify the hostname against (RFC 6125 deprecated
# CN-based hostname matching; Python's ssl module, like every modern
# client, checks SANs only).
resource "aws_acm_certificate" "this" {
  domain_name               = "authz.internal"
  subject_alternative_names = [aws_lb.this.dns_name]
  certificate_authority_arn = var.private_ca_arn
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.this.arn

  # mTLS cutover (2026-09-22, plan section 35): mode is "off" until
  # both real callers (gateway-api, platform-control-plane's backend)
  # are confirmed presenting a valid client cert -- flipping straight
  # to "verify" before that would reject every request on this
  # service's only synchronous call path with no fallback. The AWS
  # provider REJECTS trust_store_arn (and every other mutual_authentication
  # argument) unless mode is exactly "verify" -- confirmed against the
  # provider schema, not guessed at -- so aws_lb_trust_store.this below
  # is created now but deliberately left unreferenced here until that
  # cutover.
  mutual_authentication {
    mode = "off"
  }

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

# --- mTLS trust store (plan section 35, P1 production hardening) -----
#
# Client certs (gateway-api's, control-plane backend's -- each issued
# from this same private CA, see each repo's own environments/dev's
# client-cert resources) are verified against this bundle once
# aws_lb_listener.https's mutual_authentication is flipped to "verify".
# The bundle only needs this CA's own certificate -- both client certs
# chain directly to it, no intermediate.
#
# Hardcoded, not a data-source lookup: same "root CA cert is stable for
# its lifetime, update by hand if the CA is ever destroyed/recreated"
# convention this platform already uses for AUTHZ_CA_CERT_PEM in both
# gateway-api's and control-plane's own environments/dev/main.tf.
resource "aws_s3_bucket" "trust_store" {
  bucket = "${var.name_prefix}-mtls-trust-store"
}

# Required by aws_lb_trust_store -- ALB reads the CA bundle by a
# specific object version, not just "latest".
resource "aws_s3_bucket_versioning" "trust_store" {
  bucket = aws_s3_bucket.trust_store.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "trust_store" {
  bucket                  = aws_s3_bucket.trust_store.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "ca_bundle" {
  bucket = aws_s3_bucket.trust_store.id
  key    = "ca-bundle.pem"
  content = chomp(<<-EOT
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
}

resource "aws_lb_trust_store" "this" {
  name                             = "${var.name_prefix}-mtls-trust"
  ca_certificates_bundle_s3_bucket = aws_s3_bucket.trust_store.id
  ca_certificates_bundle_s3_key    = aws_s3_object.ca_bundle.key

  depends_on = [aws_s3_bucket_versioning.trust_store]
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Read-only, deliberately: this service never writes a principal
# mapping (onboarding/provisioning.py in this repo's app/ half owns that
# write path) -- see the module docstring and README.md's "read-only"
# note.
resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "principal_mappings_read" {
  statement {
    sid       = "ReadProvisionedPrincipalMappings"
    actions   = ["dynamodb:GetItem", "dynamodb:Scan"]
    resources = [var.provisioned_principal_mappings_table_arn]
  }
}

resource "aws_iam_role_policy" "task_principal_mappings" {
  name   = "${var.name_prefix}-principal-mappings-read"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.principal_mappings_read.json
}

# --- Tracing: ADOT sidecar -> X-Ray (see modules/ecs_service's own
# identical comment for why AOT_CONFIG_CONTENT + essential=false). ----
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
      name      = "authz"
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
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "authz"
        }
      }
    },
    {
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

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "authz"
    container_port   = var.container_port
  }

  # Same reasoning as modules/ecs_service: CI deploys by registering a
  # new task definition revision directly, outside Terraform.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.https]

  tags = {
    Environment = var.environment
  }
}

# Plan section 35.6 -- same shape as modules/ecs_service's own
# autoscaling target/policy. gateway-api calls this synchronously on
# every AWS_IAM request when configured (AUTHZ_SERVICE_URL) -- if this
# service can't scale, it becomes the platform's throughput ceiling
# regardless of how much the gateway itself scales.
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

# --- Operational alarms (plan section 35's P1 hardening) -----------------
# Same reasoning as modules/ecs_service's own copy of these three --
# this service is on gateway-api's synchronous request path (called
# per AWS_IAM request when AUTHZ_SERVICE_URL is configured), so its
# own 5xx/latency/health matters just as much as the gateway's.

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name          = "${var.name_prefix}-target-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "authz-service is returning 5xx -- gateway-api's AWS_IAM auth path degrades for every caller while this fires."
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
  alarm_description   = "At least one authz-service task is failing its ALB health check."
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
    TargetGroup  = aws_lb_target_group.this.arn_suffix
  }
  alarm_actions = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
  ok_actions    = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
}
