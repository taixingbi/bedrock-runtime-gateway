# bedrock-runtime-gateway

The Bedrock inference data plane: `gateway-api` + the M7 worker
(`app/`), and every piece of Terraform they run on (`infra/`) — ECS/
Fargate, ALB, ECR, the account-wide GitHub OIDC provider + every
repo's OIDC roles, Cognito, CloudWatch/SNS, KMS.

## Why one repo again

Platform restructuring Phase 2 (2026-09-21): merges
`bedrock-runtime-gateway-app` and `bedrock-runtime-gateway-infra`
(themselves a 2026-09-20 split of the original monolithic
`bedrock-gateway-platform`) back into one repo — an app/infra split
made sense while the platform was being pulled apart into its current
six-repo shape, but kept as two permanent repos going forward would
mean this data plane's own deploy pipeline and Terraform live in
different places for no real ownership reason (both are the SAME
team's SAME responsibility, unlike e.g. `platform-authz-service` or
`platform-edge-gateway`, which are genuinely separate services with
separate ownership boundaries).

`app/` and `infra/` are independently deployable: `.github/workflows/
app-ci.yml`/`app-promote-prod.yml` handle the application (tests,
Docker build, deploy to `gateway-{dev,prod}` ECS services);
`infra-ci.yml`/`infra-promote-prod.yml` handle Terraform
(fmt/validate/plan/apply). Each triggers only on changes to its own
subdirectory — a Terraform-only change never runs the Python test
suite, and vice versa.

No Terraform state moved as part of this merge — `infra/environments/
*/backend.tf`'s S3 state keys (`bedrock-gateway-infra/{env}/
terraform.tfstate`) are an independent namespace, never tied to the
GitHub repo name; every resource `infra/` manages is exactly the same
resource, same ARN, before and after this merge. Verified via
`terraform plan` in every environment: no changes, before any config
edits.

See `app/README.md` and `infra/README.md` for the full detail on each
half. This repo is one of six in the platform:

- **bedrock-runtime-gateway** (this repo) — inference data plane.
- [platform-edge-gateway](https://github.com/taixingbi/platform-edge-gateway) — ingress: API Gateway, SigV4/AuthN, VPC Link, global throttling.
- [platform-authz-service](https://github.com/taixingbi/platform-authz-service) — AuthZ decision service (RBAC/ABAC).
- [platform-control-plane](https://github.com/taixingbi/platform-control-plane) — admin + onboarding + portal.
- [platform-policy-definitions](https://github.com/taixingbi/platform-policy-definitions) — policy schema/templates/defaults.
- `platform-foundation` — planned Phase 3: shared account-level infra (VPC, GitHub OIDC provider, account-wide bootstrap IAM) carved out of `infra/`, once this merge is stable.
