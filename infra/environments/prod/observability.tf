# Central observability for the metrics services/gateway/telemetry/
# metrics.py emits via CloudWatch Embedded Metric Format (EMF) --
# gateway-api's own structured logs (already shipped to this
# environment's CloudWatch Logs) are extracted into real custom
# metrics automatically, no new AWS service and no new IAM grant
# needed (EMF extraction is server-side, on log data the app already
# has logs:PutLogEvents for).
#
# Global AND per-tenant views come from the SAME metric emission (see
# metrics.py's own docstring on its dual dimension-set design): a
# metric with just the `environment` dimension is the global-per-
# environment rollup; the same metric with `environment` + `tenant_id`
# is the per-tenant breakdown, retrieved here via a CloudWatch Metrics
# Insights SEARCH expression rather than one hardcoded widget per
# tenant, so adding a new tenant needs no dashboard change.
#
# `environment` is always a real dimension, never omitted -- this
# account/region has ONE shared BedrockGateway namespace across dev
# and prod, so every widget/alarm below filters to this environment's
# value explicitly. Omitting it would silently blend dev and prod
# metrics together the moment prod carries traffic.

locals {
  observability_namespace   = "BedrockGateway"
  observability_environment = "prod"
}

resource "aws_cloudwatch_dashboard" "gateway" {
  dashboard_name = "${local.name_prefix}-gateway"

  dashboard_body = jsonencode({
    widgets = [
      {
        type       = "text", x = 0, y = 0, width = 24, height = 1,
        properties = { markdown = "# Bedrock Gateway (prod) -- Global" }
      },
      {
        type = "metric", x = 0, y = 1, width = 8, height = 6,
        properties = {
          title  = "Request volume (global)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.observability_namespace, "RequestCount", "environment", local.observability_environment, { stat = "Sum", period = 60, label = "Requests" }]
          ]
        }
      },
      {
        type = "metric", x = 8, y = 1, width = 8, height = 6,
        properties = {
          title  = "Error / reject rate (global, %)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.observability_namespace, "ErrorCount", "environment", local.observability_environment, { stat = "Sum", period = 60, id = "errors", visible = false }],
            [local.observability_namespace, "RejectCount", "environment", local.observability_environment, { stat = "Sum", period = 60, id = "rejects", visible = false }],
            [local.observability_namespace, "RequestCount", "environment", local.observability_environment, { stat = "Sum", period = 60, id = "requests", visible = false }],
            [{ expression = "100 * errors / requests", label = "Error rate %", id = "error_rate" }],
            [{ expression = "100 * rejects / requests", label = "Reject rate %", id = "reject_rate" }],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 1, width = 8, height = 6,
        properties = {
          title  = "Estimated cost (global, USD)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.observability_namespace, "EstimatedCostUsd", "environment", local.observability_environment, { stat = "Sum", period = 300, label = "Cost/5min" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 7, width = 12, height = 6,
        properties = {
          title  = "E2E latency (global, ms)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.observability_namespace, "E2ELatencyMs", "environment", local.observability_environment, { stat = "p50", period = 60, label = "p50" }],
            [local.observability_namespace, "E2ELatencyMs", "environment", local.observability_environment, { stat = "p95", period = 60, label = "p95" }],
            [local.observability_namespace, "E2ELatencyMs", "environment", local.observability_environment, { stat = "p99", period = 60, label = "p99" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 7, width = 12, height = 6,
        properties = {
          title  = "TTFT -- streaming only (global, ms)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [local.observability_namespace, "TTFTMs", "environment", local.observability_environment, { stat = "p50", period = 60, label = "p50" }],
            [local.observability_namespace, "TTFTMs", "environment", local.observability_environment, { stat = "p95", period = 60, label = "p95" }],
            [local.observability_namespace, "TTFTMs", "environment", local.observability_environment, { stat = "p99", period = 60, label = "p99" }],
          ]
        }
      },
      {
        type       = "text", x = 0, y = 13, width = 24, height = 1,
        properties = { markdown = "# Per-tenant (auto-discovers every tenant_id -- no dashboard change needed to add one)" }
      },
      {
        type = "metric", x = 0, y = 14, width = 12, height = 6,
        properties = {
          title  = "E2E latency p95 by tenant (ms)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [{ expression = "SEARCH('{${local.observability_namespace},environment,tenant_id} MetricName=\"E2ELatencyMs\" environment=\"${local.observability_environment}\"', 'p95', 60)", label = "" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 14, width = 12, height = 6,
        properties = {
          title  = "Request volume by tenant"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [{ expression = "SEARCH('{${local.observability_namespace},environment,tenant_id} MetricName=\"RequestCount\" environment=\"${local.observability_environment}\"', 'Sum', 60)", label = "" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 20, width = 12, height = 6,
        properties = {
          title  = "Cost by tenant (USD/5min)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [{ expression = "SEARCH('{${local.observability_namespace},environment,tenant_id} MetricName=\"EstimatedCostUsd\" environment=\"${local.observability_environment}\"', 'Sum', 300)", label = "" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 20, width = 12, height = 6,
        properties = {
          title  = "TTFT p95 by tenant -- streaming only (ms)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            [{ expression = "SEARCH('{${local.observability_namespace},environment,tenant_id} MetricName=\"TTFTMs\" environment=\"${local.observability_environment}\"', 'p95', 60)", label = "" }]
          ]
        }
      },
    ]
  })
}

# Thresholds below are a starting point, not tuned against real
# traffic -- deliberately documented as adjustable rather than
# presented as validated SLOs. treat_missing_data = "notBreaching" on
# all of them: no traffic in a period must never look like an outage.

resource "aws_cloudwatch_metric_alarm" "global_error_rate" {
  alarm_name          = "${local.name_prefix}-global-error-rate"
  alarm_description   = "Global ErrorCount/RequestCount exceeded 10% for 3 consecutive minutes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 10 # percent
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "error_rate"
    expression  = "100 * errors / requests"
    label       = "Error rate %"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = local.observability_namespace
      metric_name = "ErrorCount"
      period      = 60
      stat        = "Sum"
      dimensions = {
        environment = local.observability_environment
      }
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = local.observability_namespace
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"
      dimensions = {
        environment = local.observability_environment
      }
    }
  }

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
  ok_actions    = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "global_reject_rate" {
  alarm_name          = "${local.name_prefix}-global-reject-rate"
  alarm_description   = "Global RejectCount/RequestCount exceeded 30% for 3 consecutive minutes -- widespread admission-control pressure (rate/TPM/budget/concurrency/kill-switch/model-quota), not a downstream failure."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 30 # percent
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "reject_rate"
    expression  = "100 * rejects / requests"
    label       = "Reject rate %"
    return_data = true
  }
  metric_query {
    id = "rejects"
    metric {
      namespace   = local.observability_namespace
      metric_name = "RejectCount"
      period      = 60
      stat        = "Sum"
      dimensions = {
        environment = local.observability_environment
      }
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = local.observability_namespace
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"
      dimensions = {
        environment = local.observability_environment
      }
    }
  }

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
  ok_actions    = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "global_e2e_latency_p95" {
  alarm_name          = "${local.name_prefix}-global-e2e-latency-p95"
  alarm_description   = "Global E2E latency p95 exceeded 5s for 3 consecutive minutes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "E2ELatencyMs"
  namespace           = local.observability_namespace
  period              = 60
  extended_statistic  = "p95"
  threshold           = 5000 # ms
  treat_missing_data  = "notBreaching"
  dimensions = {
    environment = local.observability_environment
  }

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
  ok_actions    = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "global_ttft_p95" {
  alarm_name          = "${local.name_prefix}-global-ttft-p95"
  alarm_description   = "Global TTFT (streaming) p95 exceeded 2s for 3 consecutive minutes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "TTFTMs"
  namespace           = local.observability_namespace
  period              = 60
  extended_statistic  = "p95"
  threshold           = 2000 # ms
  treat_missing_data  = "notBreaching"
  dimensions = {
    environment = local.observability_environment
  }

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
  ok_actions    = [aws_sns_topic.ops_alerts.arn]
}
