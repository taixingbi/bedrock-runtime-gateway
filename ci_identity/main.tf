# This repo's own CI identity (app deploy-dev/prod, infra
# plan/apply-dev/apply-prod) -- Terraform-ownership migration from
# platform-foundation (2026-09-21), step 4 of 6 (the largest: 5 roles,
# following AuthZ, Control Plane, and Edge Gateway). These 5 IAM roles
# already existed, created and managed by
# platform-foundation/environments/global's module "github_oidc_app" /
# "github_oidc_infra" calls. Moved here via `terraform import` (never
# delete/recreate) so this repo owns its own CI permissions going
# forward. ARNs are unchanged; this repo's GitHub Environment variables
# do not need to change.
#
# The account-wide OIDC provider itself stays owned by
# platform-foundation (module.github_oidc_foundation, after this same
# migration moved that one resource's ownership there from this
# repo's own former module.github_oidc_infra call) -- this repo, like
# every other, only ever references it via data source.
#
# A separate Terraform root from infra/environments/{dev,prod} -- own
# state file, own (infrequent) apply.

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

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# --- App repo: push to ECR, deploy to ECS. ---------------------------

data "aws_iam_policy_document" "app_deploy" {
  for_each = { dev = "gateway-dev", prod = "gateway-prod" }

  statement {
    sid = "PushToEcr"
    actions = [
      "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability",
      "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
    ]
    resources = ["arn:aws:ecr:${var.aws_region}:${local.account_id}:repository/${each.value}*"]
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "DeployToEcs"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${var.aws_region}:${local.account_id}:cluster/${each.value}*"]
    }
  }

  # Not cluster-scoped (task definitions are cluster-independent), so
  # the ecs:cluster condition above can't apply to these two.
  statement {
    sid       = "RegisterTaskDefinition"
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid     = "PassEcsRoles"
    actions = ["iam:PassRole"]
    resources = [
      "arn:aws:iam::${local.account_id}:role/${each.value}*-execution",
      "arn:aws:iam::${local.account_id}:role/${each.value}*-task",
    ]
  }
}

# --- This repo's own Terraform plan (read-only, safe on every PR) and
# apply (read-write, merge-only/promote-prod). Most of the services
# this repo's Terraform manages (EC2/VPC, ELBv2, ECS, ECR, API Gateway
# v2) don't support resource-level IAM scoping on their creation
# actions -- a vpc/subnet/security-group/ALB/etc. ARN doesn't exist
# until after it's created, so IAM can't restrict *which* one a
# CreateX call is allowed to make. Broad service-level grants
# (Resource "*") for those is standard practice for a Terraform CI
# role, not an oversight; IAM role management and the Terraform state
# backend *do* support real resource scoping, so those are scoped by
# name below. --------------------------------------------------------

data "aws_iam_policy_document" "infra_plan" {
  statement {
    sid = "ReadOnly"
    actions = [
      "ec2:Describe*",
      "elasticloadbalancing:Describe*",
      "ecs:Describe*", "ecs:List*",
      "ecr:Describe*", "ecr:List*", "ecr:GetLifecyclePolicy",
      "apigateway:GET",
      "logs:Describe*", "logs:List*",
      "iam:Get*", "iam:List*",
      "sts:GetCallerIdentity",
      # Describe*, not just DescribeTable: refreshing an
      # aws_dynamodb_table's full state also calls
      # DescribeContinuousBackups (PITR), DescribeTimeToLive, etc.
      "dynamodb:GetItem", "dynamodb:Describe*", "dynamodb:ListTagsOfResource",
      "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ListQueues", "sqs:ListQueueTags",
      "s3:GetObject", "s3:ListBucket",
      # aws_s3_bucket + its sub-resources (public access block,
      # encryption, lifecycle, CORS, ...) call many distinct Get*
      # bucket-config actions on refresh.
      "s3:Get*",
      # Bedrock guardrail refresh (aws_bedrock_guardrail.this).
      "bedrock:Get*", "bedrock:List*",
      # Refreshing aws_cognito_user/aws_cognito_user_in_group state
      # calls the Admin* variants (AdminGetUser,
      # AdminListGroupsForUser), a separate action namespace from
      # Get*/List* despite reading the same data.
      "cognito-idp:Describe*", "cognito-idp:Get*", "cognito-idp:List*",
      "cognito-idp:AdminGetUser", "cognito-idp:AdminListGroupsForUser",
      "cloudfront:Get*", "cloudfront:List*",
      # Not covered by ec2:Describe* -- a distinct action name for the
      # same read, needed to plan module.portal_service's prefix-list
      # ingress rule.
      "ec2:GetManagedPrefixListEntries",
      # Internal TLS (gateway-api <-> authz-service): the private CA
      # and the ACM cert it issues.
      "acm-pca:Describe*", "acm-pca:Get*", "acm-pca:List*",
      "acm:Describe*", "acm:Get*", "acm:List*",
      # mTLS client cert delivery (plan section 35): refreshing
      # aws_secretsmanager_secret/secret_version state -- GetResourcePolicy
      # is a distinct action from DescribeSecret, called separately to
      # check for a resource-based policy on the secret (learned live:
      # DescribeSecret alone still 403s on this one).
      "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue",
      "secretsmanager:ListSecretVersionIds", "secretsmanager:ListSecrets",
      "secretsmanager:GetResourcePolicy",
      "application-autoscaling:Describe*", "application-autoscaling:ListTagsForResource",
      "cloudwatch:Describe*", "cloudwatch:List*", "cloudwatch:Get*",
      "sns:GetTopicAttributes", "sns:ListTagsForResource", "sns:ListTopics",
      "kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus",
      "kms:ListResourceTags", "kms:ListAliases",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "infra_apply" {
  statement {
    sid       = "Ec2Broad"
    actions   = ["ec2:*"]
    resources = ["*"]
  }
  statement {
    sid       = "ElbBroad"
    actions   = ["elasticloadbalancing:*"]
    resources = ["*"]
  }
  statement {
    sid       = "EcsBroad"
    actions   = ["ecs:*"]
    resources = ["*"]
  }
  statement {
    sid       = "EcrBroad"
    actions   = ["ecr:*"]
    resources = ["*"]
  }
  statement {
    sid       = "ApiGatewayBroad"
    actions   = ["apigateway:*"]
    resources = ["*"]
  }
  statement {
    sid       = "LogsBroad"
    actions   = ["logs:*"]
    resources = ["*"]
  }
  # SQS/DynamoDB resource ARNs (queue URL, table name) don't exist
  # until creation, same reasoning as every other broad grant above --
  # not scopable ahead of time.
  statement {
    sid       = "SqsBroad"
    actions   = ["sqs:*"]
    resources = ["*"]
  }
  statement {
    sid       = "DynamoDbBroad"
    actions   = ["dynamodb:*"]
    resources = ["*"]
  }
  # Cognito: User Pool/domain/client/group ids don't exist until
  # creation either -- same reasoning as SqsBroad/DynamoDbBroad.
  statement {
    sid       = "CognitoBroad"
    actions   = ["cognito-idp:*"]
    resources = ["*"]
  }
  # CloudFront distribution ids likewise don't exist until creation;
  # ListCachePolicies/ListOriginRequestPolicies (looking up AWS's
  # managed policies by name) need read access even during plan.
  statement {
    sid       = "CloudFrontBroad"
    actions   = ["cloudfront:*"]
    resources = ["*"]
  }
  # Internal TLS (gateway-api <-> authz-service): creating/activating
  # the private CA and issuing authz-service's ALB cert from it.
  statement {
    sid       = "AcmPcaBroad"
    actions   = ["acm-pca:*"]
    resources = ["*"]
  }
  statement {
    sid       = "AcmBroad"
    actions   = ["acm:*"]
    resources = ["*"]
  }
  # mTLS client cert delivery (plan section 35): secret names don't
  # exist until creation, same reasoning as every other broad grant
  # above -- not scopable ahead of time.
  statement {
    sid       = "SecretsManagerBroad"
    actions   = ["secretsmanager:*"]
    resources = ["*"]
  }

  # IAM role names ARE predictable ahead of time (unlike VPC/ALB/etc.
  # IDs), so this one can actually be scoped by name.
  statement {
    sid = "ManageGatewayAndOidcRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
      "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies", "iam:TagRole", "iam:UntagRole", "iam:PassRole",
    ]
    resources = [
      "arn:aws:iam::${local.account_id}:role/gateway-*",
      "arn:aws:iam::${local.account_id}:role/gha-*",
    ]
  }
  statement {
    sid = "ManageOidcProvider"
    actions = [
      "iam:CreateOpenIDConnectProvider", "iam:GetOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint", "iam:TagOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviders", "iam:DeleteOpenIDConnectProvider",
    ]
    # The provider resource's ARN is account+host, not name-based --
    # nothing narrower to scope this to. Retained here even though this
    # repo no longer owns the provider resource itself (moved to
    # platform-foundation's own module.github_oidc_foundation) --
    # historical grant, harmless to keep, matches every other apply
    # role's identical statement.
    resources = ["*"]
  }
  statement {
    sid = "TerraformStateS3"
    # S3-native state locking (use_lockfile) -- DeleteObject releases
    # the <key>.tflock object an apply creates to hold the lock. Not
    # needed by the plan role above -- every plan job always runs with
    # -lock=false, so it never touches the lock file at all.
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::*tfstate*", "arn:aws:s3:::*tfstate*/*"]
  }
  # S3AuditStore's bucket (gateway-{dev,prod}-audit). Bucket name IS
  # predictable (this app's own naming convention), so scoped by name
  # rather than "*" like the tfstate grant above would need to be if
  # extended here too. Widened to "gateway-*-audit*" to also cover
  # S3RequestAuditStore's bucket (gateway-{dev,prod}-audit-immutable).
  statement {
    sid       = "S3AuditBucketBroad"
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::gateway-*-audit*", "arn:aws:s3:::gateway-*-audit*/*"]
  }
  # BedrockGuardrailClient's aws_bedrock_guardrail. Guardrail ids don't
  # exist until creation, same "*" reasoning as
  # SqsBroad/DynamoDbBroad/CognitoBroad above.
  statement {
    sid = "BedrockGuardrailBroad"
    actions = [
      "bedrock:CreateGuardrail", "bedrock:CreateGuardrailVersion", "bedrock:UpdateGuardrail",
      "bedrock:DeleteGuardrail", "bedrock:GetGuardrail", "bedrock:ListGuardrails",
      "bedrock:TagResource", "bedrock:UntagResource", "bedrock:ListTagsForResource",
    ]
    resources = ["*"]
  }
  # ECS service autoscaling targets/policies and the CloudWatch alarms
  # driving the worker's step-scaling. application-autoscaling's own
  # resource_id is a composite string (service/cluster/service-name),
  # not a separately scopable ARN.
  statement {
    sid       = "AppAutoscalingBroad"
    actions   = ["application-autoscaling:*"]
    resources = ["*"]
  }
  statement {
    sid       = "CloudWatchBroad"
    actions   = ["cloudwatch:*"]
    resources = ["*"]
  }
  # An SNS topic for CloudWatch alarm notifications (5xx rate,
  # latency, DLQ depth, ...). Topic ARNs don't exist until creation,
  # same "*" reasoning as every other broad grant here.
  statement {
    sid       = "SnsBroad"
    actions   = ["sns:*"]
    resources = ["*"]
  }
  # application-autoscaling needs an IAM service-linked role to
  # actually call ecs:UpdateService on the platform's behalf --
  # created automatically on first use IF the caller has this
  # permission; scoped to the one service-linked role name AWS uses
  # for this, not IamBroad.
  statement {
    sid       = "AppAutoscalingServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/ecs.application-autoscaling.amazonaws.com/*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["ecs.application-autoscaling.amazonaws.com"]
    }
  }
  # A shared CMK for both audit buckets (SSE-KMS instead of
  # SSE-S3/AES256, for CloudTrail attribution of who decrypted/
  # generated a data key). Key ids/aliases don't exist until creation,
  # same "*" reasoning as every other broad grant here.
  statement {
    sid       = "KmsBroad"
    actions   = ["kms:*"]
    resources = ["*"]
  }
}

module "github_oidc_app" {
  source = "git::https://github.com/taixingbi/platform-foundation.git//modules/github_oidc?ref=main"

  # The account-wide OIDC provider is owned by platform-foundation
  # (module.github_oidc_foundation) -- every other repo, this one
  # included, only ever references it via data source.
  create_oidc_provider = false
  github_org           = var.github_org
  github_repo          = "bedrock-runtime-gateway"

  roles = {
    dev = {
      role_name   = "gha-app-deploy-dev"
      policy_json = data.aws_iam_policy_document.app_deploy["dev"].json
    }
    prod = {
      role_name   = "gha-app-deploy-prod"
      policy_json = data.aws_iam_policy_document.app_deploy["prod"].json
    }
  }
}

module "github_oidc_infra" {
  source = "git::https://github.com/taixingbi/platform-foundation.git//modules/github_oidc?ref=main"

  create_oidc_provider = false
  github_org           = var.github_org
  github_repo          = "bedrock-runtime-gateway"

  roles = {
    plan = {
      role_name   = "gha-infra-plan"
      policy_json = data.aws_iam_policy_document.infra_plan.json
    }
    apply-dev = {
      role_name   = "gha-infra-apply-dev"
      policy_json = data.aws_iam_policy_document.infra_apply.json
    }
    apply-prod = {
      role_name   = "gha-infra-apply-prod"
      policy_json = data.aws_iam_policy_document.infra_apply.json
    }
  }
}
