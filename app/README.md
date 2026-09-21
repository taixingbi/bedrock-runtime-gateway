# bedrock-runtime-gateway / app

Multi-tenant Enterprise LLM Gateway on AWS Bedrock — the application
code, and the core of a production-shaped enterprise AI platform, not
just the original walking skeleton. **M0–M6 (the original V1 build)
are complete**, and the platform has grown well past them since —
see `docs/ROADMAP.md` for the M0–M6 milestone breakdown; the summary
below covers everything real since then.

- **M0–M6** — chat endpoint, OIDC/JWT + AWS_IAM (SigV4) auth, RBAC, tenant
  policy plane (kill switch, rate limit, model allowlist), input/output
  guardrails, response cache + circuit breaker + certified fallback
  routing, SSE streaming, OpenTelemetry tracing, cost/SLO tracking,
  load/chaos test coverage.
- **M7** — async jobs (SQS-style queue + worker + store), `api/jobs_routes.py`.
- **M8** — FinOps: per-tenant/per-application budgets and usage/cost tracking (`usage/store.py`).
- **M9** — model lifecycle/certification gate (`routing/certification.py`, `policies/certified_models.yaml`).
- **M11** — self-service application onboarding: submit → approve/reject → inline provisioning, no Terraform/redeploy (`onboarding/*`, `api/onboarding_routes.py`).
- **M12** — delegates AWS_IAM principal resolution and (since plan 35.16) resource/context-aware authorization to `platform-authz-service`'s real PDP over HTTP, when `AUTHZ_SERVICE_URL` is configured (`auth/aws_iam.py`'s `HttpIamTenantResolver`).
- **Plan section 33** — tenant policy versioning: propose → approve/reject → apply, with a DynamoDB history table backing rollback (`policy/change_requests.py`); the portal has a full UI for this.
- **Plan section 34** — enterprise IdP group→tenant/role mapping (`auth/enterprise_groups.py`), PDP-style structured authorization decisions with a `decision_id` threaded into every audit event (`authz/decision.py`), a model governance registry with data-classification enforcement (`routing/model_registry.py`), a unified admission-control decision (kill switch + rate limit + budget as one), and cost governance.
- **Plan sections 35 (P0–P2 production hardening)** — private-subnet networking, ECS autoscaling, a real cross-service PDP call carrying resolved model/resource/data-classification (not just identity), distributed (DynamoDB-backed) concurrency limiting and rate limiting with a TTL'd lease design (self-healing after a crashed task), fail-closed model governance and authz defaults for prod, CI security scanning (CodeQL, pip-audit, Trivy), and more — see `plan.md` in the platform root for the full list.

## This repo vs. the platform's other repos

This is the `app/` half of `bedrock-runtime-gateway` -- see the
[top-level README](../README.md) for why app/ and infra/ live in one
repo again (they were briefly split, 2026-09-20/21). The platform's
other repos:

- [platform-policy-definitions](https://github.com/taixingbi/platform-policy-definitions) — canonical tenant/route/IAM-principal policy config, schema-validated in CI.
- [bedrock-gateway-portal](https://github.com/taixingbi/bedrock-gateway-portal) — the admin/control-plane UI (Next.js): tenant management, onboarding approval, policy propose/approve/reject/rollback, usage/cost. Being superseded by `platform-control-plane`'s own portal.
- [platform-authz-service](https://github.com/taixingbi/platform-authz-service) — the centralized authorization PDP this app delegates to on the AWS_IAM path.
- [platform-edge-gateway](https://github.com/taixingbi/platform-edge-gateway) — the API Gateway/VPC Link front door.
- [platform-control-plane](https://github.com/taixingbi/platform-control-plane) — admin + onboarding backend (in progress) + portal.

`policies/` in this repo is a **copy**, not the source of truth — see
its banner comment and `scripts/sync-policies.sh`. A real DynamoDB-
backed `PolicyStore` now exists (`services/gateway/policy/store.py`'s
`DynamoDbPolicyStore`, layered via `LayeredPolicyStore` with this
file-based copy as fallback) and is live today for onboarding-
provisioned tenants (M11); `scripts/migrate_file_tenants_to_dynamodb.py`
is the (dry-run-by-default) backfill tool for hand-managed tenants
still living only in this file. The two-source-of-truth question this
implies (Git-authored file vs. DynamoDB-authored via the portal) is a
known, tracked gap — see `plan.md` section 35 in the platform root.

## Quickstart (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
poetry install                       # installs runtime and dev dependencies
cp .env.example .env                  # adjust if needed; defaults are fine for local dev
export $(grep -v '^#' .env | xargs)   # or use direnv/dotenv-cli

# AWS credentials come from your normal chain (env vars / ~/.aws/credentials
# / SSO / instance role) — not from .env. You need `bedrock:InvokeModel`
# (or equivalent Converse permission) on the target model in BEDROCK_MODEL_ID,
# and model access enabled for that model in the Bedrock console for your region.

python -m services.gateway.main
# -> gateway-api listening on http://0.0.0.0:8080
```

No real OIDC provider is configured by default (`OIDC_JWKS_URL` is
empty), so the server verifies tokens against a local dev keypair it
generates on first run (`.dev/jwt_keypair.json`, gitignored). Mint a
matching token with `scripts/generate_dev_token.py`:

```bash
curl -s http://localhost:8080/healthz   # unauthenticated liveness probe

# tenant_id must be one already configured in policies/tenants.yaml
# (finance / search / sandbox / team-a out of the box) -- see M2 below.
TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id finance --roles developer)

curl -s -X POST http://localhost:8080/v1/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello in one sentence."}]}'
```

To verify against a real IdP instead, set `OIDC_JWKS_URL`, `OIDC_ISSUER`,
and `OIDC_AUDIENCE` and the server switches to `JwksVerifier` automatically
(see `services/gateway/auth/jwt_verifier.py`). To call in as an AWS IAM
principal instead of a JWT holder, see `services/gateway/auth/aws_iam.py`
and `policies/iam_tenants.yaml` — in production this only works through
platform-edge-gateway's API Gateway `/iam/*` route, which is the thing
that actually verifies SigV4 and injects the identity headers this app
trusts.

Tenant policy lives in `policies/tenants.yaml` (state, model allowlist,
rate limit, guardrail policy, route set — see M2 below). To flip a
tenant's state at runtime (the kill switch) without restarting the
server:

```bash
ADMIN_TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id platform --roles platform_admin)

curl -s -X PUT http://localhost:8080/v1/admin/tenants/finance/state \
  -H "authorization: Bearer $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"state": "SUSPENDED"}'
```

For an SSE stream instead of a single JSON response, pass `"stream": true`:

```bash
curl -N -s -X POST http://localhost:8080/v1/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"stream": true, "messages":[{"role":"user","content":"Count to five."}]}'
```

## Tests

No real AWS calls, no network — a fake `ConverseClient` stands in for
Bedrock (see `services/gateway/tests/fakes.py`), tests mint their own
locally-signed JWTs (see `services/gateway/tests/auth_fixtures.py`),
policy/rate-limit tests use a `FakeClock` (see
`services/gateway/tests/fake_clock.py`) instead of real `time.sleep()`,
and guardrail failure/timeout scenarios use a `FakeGuardrailClient` (see
`services/gateway/tests/fake_guardrail.py`) instead of racing a real
clock against a real deadline.

```bash
poetry run python -m unittest discover -s services/gateway/tests -t .
```

407 tests (requires `poetry install --with dev` — 3 of the test modules,
covering the DynamoDB-backed concurrency limiter/rate limiter and the
tenant migration script, need `moto` for real DynamoDB emulation and
fail to import without it): M0-M6 coverage (a chat span carries the
full telemetry attribute set checked against an in-memory OTel
exporter, cost/SLO-breach calculations, PII redaction round-tripping
through `DebugCaptureStore`, a grep-the-logs test proving raw message
content never appears in an operational log line) plus every
milestone/plan-section since — onboarding approval and provisioning,
policy change-request propose/approve/reject/rollback, the model
governance registry and data-classification gate (including their
fail-closed modes), the unified admission-control decision, real
resource/context-aware calls into platform-authz-service's PDP
(mocked HTTP layer here; live end-to-end verification is a real two-
container Docker test, not part of this suite), and the DynamoDB
concurrency/rate-limiter classes' actual conditional-transaction
semantics via moto rather than a hand-rolled call-recording fake.

Note: `python -m unittest` runs quietly by design -- `services/gateway/tests/__init__.py`
installs a no-op global tracer before any test imports `create_app()`, so
you won't see the console span exporter's output during the suite. Run
the server for real (`python -m services.gateway.main`) to see it.

## Load / chaos testing (M6)

```bash
poetry run python -m unittest discover -s loadtests -t .
```

5 scenarios, each firing a burst of concurrent requests at the app over
`httpx.ASGITransport` (no live server needed) with fault-injecting fakes
distinct from the unit-test ones (`loadtests/fault_injection.py` — these
inject faults across *many* concurrent calls with thread-safe counters):
circuit breaker stops a retry storm under throttling, tenant noisy-
neighbor isolation holds under simultaneous bursts, kill-switch blocks
the very next request after an admin flip, a policy_epoch bump
invalidates the cache under concurrent read/write races, and a STRICT
tenant fails closed for every one of many concurrent requests when its
guardrail is down.

Each scenario also has a human-run `locustfile.py` for real HTTP
throughput/latency against a live gateway process — including,
deliberately by hand, against real Bedrock if you want an actual quota
number (costs money, needs your AWS credentials, never run
automatically). Full instructions: `docs/LOAD_TESTING.md`.

## Docker

```bash
docker build -t gateway-api:latest .
docker run --rm -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
  gateway-api:latest
```

Runs as an ECS Fargate task behind a private ALB in a private subnet,
reachable only through platform-edge-gateway's VPC Link — see
this repo's own infra/ for that Terraform. CI here builds this image,
smoke-tests `/healthz`, runs CodeQL (SAST), `pip-audit` (dependency
CVEs), and Trivy (container image CVEs) -- findings land in the repo's
Security tab, none of them gate deployment yet -- and (on `dev`/`main`
pushes) deploys it via `gha-app-deploy-{dev,prod}` OIDC roles that this
repo's own infra/ half provisions. See `.github/workflows/app-ci.yml`.

## Layout

```
services/gateway/
  api/          # request/response schemas + route handlers: routes.py (chat), admin_routes.py,
                # jobs_routes.py (M7), onboarding_routes.py (M11)
  auth/         # JWT + AWS_IAM verification, identity, RBAC (M1); enterprise_groups.py resolves
                # an IdP groups claim to tenant/role (plan 34.2); aws_iam.py optionally delegates
                # both identity AND resource-level authorization to platform-authz-service (M12,
                # plan 35.16)
  authz/        # decision.py -- PDP-style structured Decision (allow/reason/decision_id/
                # policy_version) wrapping rbac.py's checks for uniform audit logging (plan 34.3)
  policy/       # tenant policy model/store/cache, rate limiter (M2); change_requests.py --
                # propose/approve/reject/rollback for a provisioned tenant's policy (plan 33);
                # validation.py, DynamoDbPolicyStore/DynamoDbRateLimiter (real cross-instance
                # coordination, plan 35.2)
  guardrails/   # GuardrailClient seam, basic regex impl, fail-closed enforcement (M3)
  cache/        # policy-aware response cache: key derivation + in-memory store (M4)
  routing/      # circuit breaker + certified router with fallback (M4); model_registry.py --
                # governance overlay (status/owner/max_data_classification), certification.py (M9)
  inference/    # Bedrock Converse client (retry/backoff + streaming, no boto3 at import time)
  jobs/         # SQS-style queue + worker + store for async job submission (M7)
  onboarding/   # self-service application onboarding: request, approve/reject, inline
                # provisioning of a principal mapping + tenant policy (M11)
  usage/        # per-tenant/per-application spend tracking, feeds the portal's Usage page (M8)
  concurrency.py  # BlockingCallRunner (bounded thread offload) + ConcurrencyLimiter /
                  # DynamoDbConcurrencyLimiter (fast-reject semaphore, TTL-leased + self-healing
                  # via reconcile() when distributed -- plan 35.18)
  telemetry/    # structured JSON logging + middleware, OTel tracing, cost/SLO, debug capture (M5);
                # request_audit.py -- one durable, metadata-only audit event per request (plan 34.4)
  tests/        # unit tests + fakes/fixtures (no AWS, no network needed except moto's DynamoDB
                # emulation for the DynamoDB-backed classes)
  config.py     # env -> Settings (the only module that reads os.environ)
  main.py       # app factory / entrypoint
  pipeline.py   # request pipeline stages (auth/policy/admission/guardrails/authz)
  streaming.py  # SSE + client-disconnect cancellation (M4)
scripts/
  generate_dev_token.py             # mint a local dev JWT for curl-testing
  sync-policies.sh                  # manual, human-run refresh of policies/ from a
                                     # platform-policy-definitions checkout -- not run by CI
  migrate_file_tenants_to_dynamodb.py  # dry-run-by-default backfill of policies/tenants.yaml
                                        # into DynamoDbPolicyStore, idempotent
policies/
  tenants.yaml            # COPY -- canonical source is platform-policy-definitions
  route_sets.yaml         # COPY -- ditto
  iam_tenants.yaml        # COPY -- ditto
  certified_models.yaml   # COPY -- ditto (M9)
  model_registry.yaml     # COPY -- ditto (plan 34.5)
loadtests/
  fault_injection.py       # multi-call fault-injecting fakes (M6)
  harness.py                # ASGI-direct concurrency test harness (M6)
  bedrock/ tenant/ failure/ guardrails/   # test_scenario.py (automated) + locustfile.py (manual) per area
docs/
  ROADMAP.md         # M0-M6 milestone status (see this file's own top section for M7+/plan-33-35)
  DESIGN-NOTES.md    # deviations from the plan and why
  LOAD_TESTING.md    # M6 scenarios: what they prove and how to run them
```
