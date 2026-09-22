# bedrock-runtime-gateway / infra

Terraform for everything the `app/` half of this repo (and its
sibling services in other repos) run on: VPC/networking (private
subnets + NAT + gateway VPC endpoints), ALBs, ECS/Fargate (gateway,
authz, worker services, each with autoscaling), ECR, this repo's own
CI/OIDC roles (`ci_identity/`), CloudWatch alarms + SNS, and a KMS CMK
for audit data.

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
environments/
  dev/              gateway-dev: every service above, applied and live
  prod/             gateway-prod: mirrors dev's Terraform, but NOT auto-applied (see below) and
                    missing authz_service TLS pending real decisions (plan.md section 35)
ci_identity/        This repo's own CI/OIDC roles (app deploy-dev/prod, infra
                    plan/apply-dev/apply-prod) -- own state, own (infrequent) apply. See
                    its own main.tf header for the 2026-09-21 Terraform-ownership
                    migration that moved these out of platform-foundation.
```

## Terraform-ownership migration (2026-09-21) -- IAM/OIDC and portal/Cognito both moved out

This repo used to centralize IAM/OIDC for the whole platform
(`modules/github_oidc`, called once per repo from a `environments/
global` this repo owned) and to own the self-service portal's infra
(`modules/portal_service`/`portal_cdn`/`cognito_idp`). Both have since
moved to their real long-term owners, and this repo's own copies of
all four modules were deleted (not just superseded) once nothing
referenced them any more:

- **IAM/OIDC**: the account-wide OIDC provider now lives permanently
  in `platform-foundation` (`module.github_oidc_foundation`); every
  repo, this one included, owns its own CI roles in its own
  `ci_identity/` root (see this repo's own `ci_identity/main.tf`),
  migrated via `terraform import` -- ARNs never changed.
- **Portal/Cognito**: `platform-control-plane` owns the real, live
  `portal_service`/`portal_cdn`/`cognito_idp` modules and the actual
  running portal today. `environments/dev/main.tf` here hardcodes the
  few Cognito values the gateway app still needs (`OIDC_JWKS_URL`/
  `OIDC_ISSUER`/`OIDC_AUDIENCE`) rather than referencing a module --
  see that block's own comment for why a hardcode beats a fragile
  data-source lookup here.

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
in each of `environments/{dev,prod}/` and `ci_identity/` is
**required**, not optional, before wiring up this repo's CI:

```bash
aws s3api create-bucket --bucket <your-tfstate-bucket> --region us-east-1
aws dynamodb create-table --table-name <your-tfstate-lock-table> \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

Then `terraform init -migrate-state` in each environment.

**2. Apply `platform-foundation` first, account-wide.** The GitHub
OIDC provider (account-wide singleton) now lives there, not here --
see that repo's own README for its one-time bootstrap. Every repo's
own `ci_identity/` (this one included) only ever references that
provider via data source; none of them can create their own roles
until it exists.

**3. Apply this repo's own `ci_identity/`.** Creates the 5 roles this
repo's own CI needs (`gha-app-deploy-{dev,prod}`,
`gha-infra-{plan,apply-dev,apply-prod}`):

```bash
cd ci_identity
terraform init
terraform apply -var="github_org=<your-github-org-or-username>"
```

Note the role ARNs in the output -- **this is the chicken-and-egg
step**: these roles don't exist until this first local apply creates
them, so this repo's own CI can't be the thing that creates them.
Every apply after this first one can run through CI (`ci_identity`
auto-applies on push to `main`, same as `environments/dev`).

**4. Create this repo's GitHub Environments**: `dev`, `prod` (app/
half's deploy roles, `app-ci.yml`/`app-promote-prod.yml`) -- set
`AWS_APP_DEPLOY_ROLE_ARN_DEV`/`_PROD`; `plan`, `apply-dev`, `apply-prod`
(infra/ half's own Terraform roles, `infra-ci.yml`/
`infra-promote-prod.yml`) -- set `AWS_INFRA_PLAN_ROLE_ARN`/
`AWS_INFRA_APPLY_{DEV,PROD}_ROLE_ARN`. `apply-dev` auto-applies on
every push to `main`; `apply-prod` is never auto-applied from CI (see
"hold prod" note below) -- add required reviewers regardless.

Every other repo in the platform bootstraps its own CI identity the
same way, in its own `ci_identity/` (or, for `platform-foundation`
itself, `environments/global/`) -- see each repo's own README for its
specific roles and GitHub Environment variables; this repo no longer
defines or documents any of them.

Restrict each Environment's deployment branches to `main` as a second
layer behind each workflow's own branch check.

**5. Apply `environments/dev` and `environments/prod`.** Same as
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
- The `gha-infra-apply-{dev,prod}` roles' IAM/EC2/ELBv2/ECS/ECR
  permissions are intentionally broad-but-name-scoped rather than
  minimal -- most of these services don't support resource-level IAM
  scoping on creation actions (a VPC/ALB/etc. ARN doesn't exist until
  after it's created). See the comment in `ci_identity/main.tf` above
  its own apply policy documents before tightening it further.
