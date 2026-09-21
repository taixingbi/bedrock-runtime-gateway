# One IAM role per {repo, GitHub Environment} pair, each assumable only
# by a GitHub Actions workflow run scoped to that Environment (repo
# Settings -> Environments) -- e.g. a job with `environment: prod` can
# assume the prod role, a job with `environment: dev` cannot. This is
# what lets GitHub's environment protection rules (required reviewers,
# etc.) gate a real AWS credential, not just a workflow label.
#
# Generic on purpose: this module knows nothing about ECR/ECS/Terraform/
# anything else a role might do -- the caller supplies each role's full
# permission policy as JSON (var.roles[*].policy_json). That's what lets
# one module serve the app repo's ECR-push+ECS-deploy roles, this repo's
# own Terraform plan/apply roles, and the policies repo's publish role,
# with three completely different permission shapes.
#
# The OIDC provider itself is an account-wide singleton (thumbprint
# fixed by GitHub) -- created once (var.create_oidc_provider = true on
# exactly one of this module's callers), referenced via data source on
# every other call.

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's OIDC intermediate CA thumbprint. AWS validates the full
  # chain itself now, but the field is still required at creation time.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "assume" {
  for_each = var.roles

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # StringLike (not StringEquals): GitHub's newer OIDC tokens append
    # immutable owner/repo IDs to the sub claim --
    # "repo:org@123/repo@456:environment:X" instead of the classic
    # "repo:org/repo:environment:X" -- and which format a given repo
    # gets isn't something this module controls. Match both forms, with
    # the wildcard only after an explicit "@" so this can't accidentally
    # match an unrelated org/repo sharing the same name prefix.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_org}/${var.github_repo}:environment:${each.key}",
        "repo:${var.github_org}@*/${var.github_repo}@*:environment:${each.key}",
      ]
    }
  }
}

resource "aws_iam_role" "this" {
  for_each = var.roles

  name               = each.value.role_name
  assume_role_policy = data.aws_iam_policy_document.assume[each.key].json
}

resource "aws_iam_role_policy" "this" {
  for_each = var.roles

  name   = each.value.role_name
  role   = aws_iam_role.this[each.key].id
  policy = each.value.policy_json
}
