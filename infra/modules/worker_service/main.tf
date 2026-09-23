# ECS Fargate service with no load balancer, no ALB, no inbound ingress
# at all -- this is a queue consumer, not something anything calls
# directly. Deliberately a separate module from ecs_service rather than
# a flag on it: ecs_service's ALB/target-group/listener/load_balancer{}
# block are unconditional resources coupled to the "gateway-api"
# container name, and this repo's existing convention is one narrowly-
# scoped module per concern (network/ecr/ecs_service/api_gateway) --
# forking is more in keeping with that than parametrizing ecs_service
# with an enable_alb branch through all of those resources.
#
# Runs in the same ECS cluster as gateway-api (var.cluster_name, from
# ecs_service's cluster_name output) -- one cluster per environment
# hosting multiple services, not a cluster per service.

resource "aws_cloudwatch_log_group" "this" {
  name              = var.log_group_name != "" ? var.log_group_name : "/ecs/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

# Egress only -- nothing ever initiates a connection to this task.
resource "aws_security_group" "worker" {
  name        = "${var.name_prefix}-worker"
  description = "Job worker egress only -- Bedrock, SQS, DynamoDB, ECR"
  vpc_id      = var.vpc_id

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

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Same Bedrock ARN-building logic as modules/ecs_service -- duplicated
# rather than shared, matching this repo's one-module-per-concern style
# (see that module's comment for why "us."-prefixed IDs need both the
# inference-profile ARN and the underlying foundation-model ARn in
# every region the profile can route to).
locals {
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

data "aws_iam_policy_document" "task" {
  statement {
    sid = "InvokeBedrockModels"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_model_arns
  }

  # Heartbeat visibility while this worker owns a job; no enqueue permission.
  statement {
    sid       = "ConsumeJobs"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [var.sqs_queue_arn]
  }

  statement {
    # Conditional claim/renew use UpdateItem; fenced terminal writes use PutItem.
    sid       = "JobRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [var.dynamodb_table_arn]
  }

  statement {
    sid       = "UsageRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [var.usage_table_arn]
  }
  statement {
    sid       = "TenantPolicies"
    actions   = ["dynamodb:GetItem"]
    resources = [var.tenant_policies_table_arn]
  }
  statement {
    sid       = "AdmissionControl"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"]
    resources = [var.admission_control_table_arn]
  }
  statement {
    sid       = "Guardrails"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [var.guardrail_arn]
  }

}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name_prefix}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

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
      name      = "gateway-worker"
      image     = var.image
      command   = var.command
      essential = true
      # No portMappings -- this task accepts no inbound connections.
      environment = [
        for k, v in var.container_env : { name = k, value = v }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
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
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  enable_execute_command = var.enable_execute_command

  # Same reasoning as ecs_service: CI deploys by registering a new task
  # definition revision and force-updating the service directly,
  # outside Terraform.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  tags = {
    Environment = var.environment
  }
}

# --- Autoscaling on queue depth (plan section 35.6) -----------------------
#
# No ALB in front of a worker -- ApproximateNumberOfMessagesVisible is
# the actual backlog signal, not CPU (a worker can be backlogged while
# idle-CPU between jobs). Step scaling (not target tracking) since
# there's no built-in "messages per task" predefined metric for SQS the
# way ALBRequestCountPerTarget exists for ALBs; a CloudWatch alarm on
# the raw queue depth driving discrete step adjustments is the standard
# pattern here.

resource "aws_appautoscaling_target" "this" {
  max_capacity       = var.autoscaling_max_capacity
  min_capacity       = var.autoscaling_min_capacity
  resource_id        = "service/${var.cluster_name}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "scale_out" {
  name               = "${var.name_prefix}-scale-out"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  service_namespace  = aws_appautoscaling_target.this.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 50
      scaling_adjustment          = 1
    }
    step_adjustment {
      metric_interval_lower_bound = 50
      scaling_adjustment          = 3
    }
  }
}

resource "aws_appautoscaling_policy" "scale_in" {
  name               = "${var.name_prefix}-scale-in"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  service_namespace  = aws_appautoscaling_target.this.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300 # slower to scale in than out -- avoid thrashing on a bursty queue
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "backlog_high" {
  alarm_name          = "${var.name_prefix}-backlog-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Jobs queue has a visible backlog -- scale the worker out."
  dimensions = {
    QueueName = var.sqs_queue_name
  }
  alarm_actions = [aws_appautoscaling_policy.scale_out.arn]
}

resource "aws_cloudwatch_metric_alarm" "backlog_empty" {
  alarm_name          = "${var.name_prefix}-backlog-empty"
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 5
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Jobs queue has been empty for 5 consecutive minutes -- scale the worker back in."
  dimensions = {
    QueueName = var.sqs_queue_name
  }
  alarm_actions = [aws_appautoscaling_policy.scale_in.arn]
}

# Plan section 35's P1 hardening -- DLQ depth is a genuinely
# exceptional signal (a job failed maxReceiveCount times and won't be
# retried automatically), unlike the scaling alarms above (routine,
# expected, not page-worthy) -- this one notifies, those don't.
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.name_prefix}-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "At least one job landed in the dead-letter queue -- exhausted its retries, needs manual investigation."
  treat_missing_data  = "notBreaching"
  dimensions = {
    QueueName = var.sqs_dlq_name
  }
  alarm_actions = var.sns_topic_arn != null ? [var.sns_topic_arn] : []
}
