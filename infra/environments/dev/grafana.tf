# Amazon Managed Grafana -- a visualization layer ON TOP OF the same
# BedrockGateway CloudWatch metrics observability.tf's Dashboard/Alarms
# already read. NOT a replacement: CloudWatch Alarms stay the source
# of truth for automated alerting (SNS-wired to ops_alerts) -- Grafana
# adds richer/ad hoc exploration (multi-panel correlation, templated
# variables, etc) on the SAME metrics, no new metrics pipeline.
#
# Real, ongoing AWS cost distinct from everything else in this repo's
# observability -- CloudWatch Dashboards/Alarms cost nothing beyond the
# metrics themselves; Managed Grafana bills per editor/viewer license
# (~$9/~$5 per month each) plus a workspace charge. Requested and
# confirmed explicitly, not a default part of the original
# observability build (see that decision recorded when this repo's
# CloudWatch-vs-Grafana tool choice was first made).
#
# AWS_SSO authentication (this account already has IAM Identity Center
# enabled) rather than SAML -- simplest path with no external IdP to
# wire up. aws_grafana_role_association below grants the account's one
# existing Identity Center user ADMIN access; without it the workspace
# exists but nobody can sign in.

resource "aws_iam_role" "grafana" {
  name = "${local.name_prefix}-grafana"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "grafana.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_grafana_workspace" "gateway" {
  name        = "${local.name_prefix}-gateway"
  description = "BedrockGateway observability -- CloudWatch metrics (see observability.tf's own EMF/dashboard/alarms)"

  account_access_type      = "CURRENT_ACCOUNT"
  authentication_providers = ["AWS_SSO"]
  # SERVICE_MANAGED: AWS attaches/maintains the IAM policy this role
  # needs for the data_sources listed below -- this repo doesn't have
  # to hand-maintain a CloudWatch-read policy itself.
  permission_type = "SERVICE_MANAGED"
  data_sources    = ["CLOUDWATCH"]
  role_arn        = aws_iam_role.grafana.arn
}

resource "aws_grafana_role_association" "admin" {
  workspace_id = aws_grafana_workspace.gateway.id
  role         = "ADMIN"
  # This account's one IAM Identity Center user (identitystore
  # list-users, d-90667f617b) -- hardcoded rather than a variable since
  # there's exactly one user in this account today; add more user_ids/
  # group_ids here (or a second aws_grafana_role_association for
  # EDITOR/VIEWER) as real users show up.
  user_ids = ["e4680408-3011-7059-3d53-77154dd0fca7"]
}

output "grafana_workspace_url" {
  value       = "https://${aws_grafana_workspace.gateway.id}.grafana-workspace.${var.aws_region}.amazonaws.com"
  description = "Sign in via AWS SSO (IAM Identity Center) -- add the BedrockGateway CloudWatch namespace as a data source panel source once signed in (CLOUDWATCH data source is pre-provisioned by permission_type=SERVICE_MANAGED, but individual dashboards/panels are still created by hand in the Grafana UI, not by Terraform)."
}
