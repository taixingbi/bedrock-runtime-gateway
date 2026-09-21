# bedrock-runtime-gateway / infra

Terraform for everything the `app/` half of this repo (and its
sibling services in other repos) run on: VPC/networking (private
subnets + NAT + gateway VPC endpoints), ALBs, ECS/Fargate (gateway,
authz, worker, portal services, each with autoscaling), ECR, IAM/OIDC
(for every repo in the platform, not just this one), Cognito (portal
login), CloudWatch alarms + SNS, and a KMS CMK for audit data.

History: originally split out of the combined `bedrock-gateway-platform`
repo so an app deploy never needed Terraform permissions and a
Terraform change never needed an app rebuild; briefly its own repo
(`bedrock-runtime-gateway-infra`, 2026-09-20/21) before merging back
into this one alongside `app/` (see the top-level README) -- app/infra
still deploy independently via separate CI workflows
(`app-ci.yml`/`infra-ci.yml`), just from one checkout now.
`platform-edge-gateway` (HTTP API + VPC Link, the actual front door)
lives in its own repo -- it looks up this repo's ALBs by name rather
than being a module here.

```
modules/
  network/         VPC, private subnets (NAT per AZ) + public subnets (NAT only), S3/DynamoDB
                    gateway VPC endpoints
  ecr/              ECR repo + lifecycle policy
  ecs_service/      ECS cluster, private ALB (HTTP + optional HTTPS listener), task
                    definition/service, autoscaling, CloudWatch alarms, IAM roles
  authz_service/    platform-authz-service's own ECS cluster/ALB/service (dev only today --
                    not yet deployed to prod, see plan.md section 35)
  worker_service/   M7 async-jobs worker's ECS service, SQS-backlog-driven autoscaling
  portal_service/   bedrock-gateway-portal's ECS service
  portal_cdn/       CloudFront distribution in front of the portal
  cognito_idp/      Cognito user pool + Hosted UI backing the portal's login
  github_oidc/      Generic: OIDC provider + N IAM roles trusting N {repo, GitHub Environment} pairs
environments/
  global/           Account-wide: the OIDC provider + every role for every repo in the platform
  dev/              gateway-dev: every service above, applied and live
  prod/             gateway-prod: mirrors dev's Terraform, but NOT auto-applied (see below) and
                    missing authz_service/portal TLS pending real decisions (plan.md section 35)
```

## Why this repo owns IAM/OIDC for every repo in the platform

`modules/github_oidc` is generic -- it knows how to create an OIDC
role trusting a given `{repo, GitHub Environment}` pair, and nothing
about what that role is allowed to do. `environments/global` calls it
once per repo in the platform (app, infra's own plan/apply, policies'
publish, portal, platform-authz-service, platform-edge-gateway).
Centralizing this here (rather than each repo owning its own OIDC
role) means every permission grant in the whole platform is reviewable
in one Terraform diff, not scattered across every repo's own history.

**What this still deliberately does not include:** a real IdP for the
JWT path (the app still falls back to its dev JWT keypair until one is
wired up -- Entra/Okta integration on the app side is real and tested,
but needs a real tenant's config values only the user can provide);
TLS between the gateway and platform-authz-service in prod specifically
(dev has this via a private CA; prod doesn't have that CA yet -- a
real ~$400/mo decision, not made on prod's behalf); a custom API
domain and CloudFront/WAF in front of platform-edge-gateway (WAF was
attempted and reverted -- AWS WAFv2 doesn't support API Gateway HTTP
APIs, only REST APIs, ALB, and a few others; real edge protection here
needs a CloudFront distribution, which needs the same domain decision);
and platform-authz-service isn't deployed to prod at all yet (dev
only). See `plan.md` section 35 in the platform root for the full,
current list of what's built versus what's a pending decision.

## One-time account setup

**1. State backend.** Unlike the original combined repo (which got
away with local state since only one person ever ran `terraform
apply`), this repo's CI needs shared, lockable state --
`backend.tf.example` -> `backend.tf` (S3 bucket + DynamoDB lock table)
in each of `environments/{global,dev,prod}/` is **required**, not
optional, before wiring up this repo's CI:

```bash
aws s3api create-bucket --bucket <your-tfstate-bucket> --region us-east-1
aws dynamodb create-table --table-name <your-tfstate-lock-table> \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

Then `terraform init -migrate-state` in each environment.

**2. Apply `environments/global`.** Creates the GitHub OIDC provider
(account-wide singleton) and every role across every repo in the platform:

```bash
cd environments/global
terraform init
terraform apply -var="github_org=<your-github-org-or-username>"
```

Note the role ARNs in the output -- **this is the chicken-and-egg
step**: this repo's own `gha-infra-plan`/`gha-infra-apply` roles don't
exist until this first local apply creates them, so this repo's CI
can't be the thing that creates them. Every apply after this first one
can run through CI.

**3. Create the GitHub Environments.** In this repo (bedrock-runtime-gateway):
- `dev`, `prod` -- app/ half's deploy roles (`app-ci.yml`/`app-promote-prod.yml`):
  set `AWS_APP_DEPLOY_ROLE_ARN_DEV`/`_PROD`.
- `plan`, `apply-dev`, `apply-prod` -- infra/ half's own Terraform roles
  (`infra-ci.yml`/`infra-promote-prod.yml`): set `AWS_INFRA_PLAN_ROLE_ARN`/
  `AWS_INFRA_APPLY_{DEV,PROD}_ROLE_ARN`. `apply-dev` auto-applies on every
  push to `main`; `apply-prod` is never auto-applied from CI (see "hold
  prod" note below) -- add required reviewers regardless.

  Every OTHER repo in the platform needs its own analogous Environments,
  all pointing at roles this repo's `environments/global/main.tf` defines:
- platform-policy-definitions: `publish` -- set `AWS_POLICY_PUBLISH_ROLE_ARN`
  (the role exists but is inert -- that repo's CI has no publish job yet
  and the role's own DynamoDB target is a placeholder table name; see
  platform-policy-definitions's own README for the current state).
- bedrock-gateway-portal: `dev`, `prod` -- set `AWS_PORTAL_DEPLOY_ROLE_ARN_DEV`/`_PROD`.
  Being superseded by platform-control-plane's own `dev` Environment/portal
  deploy role.
- platform-authz-service: `dev`, `prod`, plus its own `plan`/`apply-dev` for
  its own Terraform -- set `AWS_AUTHZ_DEPLOY_ROLE_ARN_DEV`/`_PROD` and
  `AWS_AUTHZ_INFRA_{PLAN,APPLY_DEV}_ROLE_ARN`.
- platform-edge-gateway: `plan`, `apply-dev`, `apply-prod` -- its own
  OIDC roles, defined in this repo's `environments/global/main.tf`
  under `github_oidc_api_gateway`.
- platform-control-plane: `dev` (portal deploy), plus `plan`/`apply-dev`
  for its own `infra/` -- set `AWS_PORTAL_DEPLOY_ROLE_ARN_DEV` and
  `AWS_CONTROL_PLANE_INFRA_{PLAN,APPLY_DEV}_ROLE_ARN`.

Restrict each Environment's deployment branches to `main` as a second
layer behind each workflow's own branch check.

**4. Apply `environments/dev` and `environments/prod`.** Same as
before the split -- creates/confirms the VPC, ECR repo, ECS
cluster/service, ALB, and API Gateway for each. If migrating from the
combined repo, this should show **zero changes** (same resource
addresses, same state) -- that's the actual proof the split didn't
touch any real infrastructure.

## Day to day

- PRs get `terraform fmt -check` + `validate` + `plan` (read-only,
  `gha-infra-plan`) automatically.
- `apply` runs on merge to `main` (`gha-infra-apply`), gated by
  whatever reviewers you configured on the `apply` GitHub Environment.
- Application deploys happen through this repo's own `app-ci.yml`, a
  separate workflow from this one (`infra-ci.yml`) -- infra/ owns the
  surrounding infrastructure, never the running image, even though
  both now live in the same checkout.
- `bedrock_model_ids` in each environment's `variables.tf` grants the
  ECS task role `bedrock:InvokeModel`/`InvokeModelWithResponseStream`
  on exactly those models/inference profiles. Keep it in sync with
  whatever `route_sets.yaml` says in platform-policy-definitions.
- The `gha-infra-apply` role's IAM/EC2/ELBv2/ECS/ECR/API-Gateway
  permissions are intentionally broad-but-name-scoped rather than
  minimal -- most of these services don't support resource-level IAM
  scoping on creation actions (a VPC/ALB/etc. ARN doesn't exist until
  after it's created). See the comment in
  `environments/global/main.tf` above `data.aws_iam_policy_document.infra_apply`
  before tightening it further.
