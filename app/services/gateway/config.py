"""Environment-driven configuration for the gateway service (M0).

Kept deliberately tiny for the walking skeleton. Later milestones add a
proper settings layer (per-tenant policy, guardrail config, etc.) -- this
module should stay the single place that reads os.environ so the rest of
the codebase never calls os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # AWS / Bedrock
    aws_region: str
    bedrock_model_id: str
    bedrock_timeout_s: float
    bedrock_max_retries: int

    # HTTP server
    host: str
    port: int

    # Telemetry
    service_name: str
    log_level: str

    # Identity fields stamped onto every structured JSON log line (see
    # telemetry/logging.py) -- distinct from service_name (which names
    # the OTel resource / logger to set the level on and already varies
    # per environment, e.g. "gateway-dev"). `service` is the bare,
    # environment-independent component identity; `environment` is
    # "dev"/"prod".
    service: str
    environment: str

    # Request handling
    max_input_chars: int

    # M1 identity
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str  # empty -> fall back to the local dev keypair (see auth/devkeys.py)
    oidc_jwks_cache_ttl_s: float
    dev_jwt_keypair_path: str
    chat_required_role: str
    iam_tenants_path: str  # AWS_IAM/SigV4 auth path -- see auth/aws_iam.py
    # Plan section 34.2: real enterprise IdP (Okta/Entra ID) `groups`
    # claim -> tenant_id/application_id/roles. Empty means no mapping
    # configured, so a token with only a `groups` claim (no direct
    # tenant_id) 401s -- see auth/enterprise_groups.py.
    enterprise_groups_path: str

    # M2 policy plane
    tenant_policy_path: str
    policy_cache_ttl_s: float

    # M9 model lifecycle -- see routing/certification.py
    certified_models_path: str
    # Plan section 34.5 -- governance overlay, separate file/loader
    # from certified_models.yaml (see routing/model_registry.py's
    # module docstring for why). Empty means no overlay: every
    # certified model is treated as APPROVED (see get_status()'s
    # permissive default), i.e. unchanged pre-34.5 behavior.
    model_registry_path: str
    # Plan section 35.3 (P0 production hardening): False (default)
    # keeps get_status()'s permissive fail-open -- correct for a
    # migration phase where introducing model_registry.yaml shouldn't
    # retroactively block every pre-existing certified model. True
    # flips to fail-closed: a model absent from the registry, or with
    # an unresolvable max_data_classification against the tenant's
    # own, is rejected rather than allowed. A real prod rollout should
    # set this True once the registry is actually curated -- same
    # per-environment-opt-in shape as GOVERNANCE-vs-COMPLIANCE mode on
    # the request-audit bucket (plan section 34.4).
    model_governance_fail_closed: bool

    # M4 gateway reliability
    route_set_config_path: str
    response_cache_ttl_s: float
    response_cache_max_entries: int
    circuit_breaker_failure_threshold: int
    circuit_breaker_reset_timeout_s: float

    # M5 observability
    otel_exporter_otlp_endpoint: str
    debug_capture_ttl_s: float
    # S3-backed audit store (telemetry/debug_capture.py's S3AuditStore) --
    # empty means disabled, same "empty string = off" convention as
    # jobs_queue_url/jobs_table_name below.
    audit_bucket_name: str
    # Plan section 34.4 -- SEPARATE bucket from audit_bucket_name
    # above: this one is metadata-only (no prompt/response text) and
    # always-on, not opt-in via debug_capture_enabled. Empty disables
    # it -- request_audit_store falls back to an in-memory store
    # (see main.py), same "empty string = off" convention.
    request_audit_bucket_name: str
    # BedrockGuardrailClient -- empty guardrail_id means disabled,
    # falls back to BasicGuardrailClient (see main.py). Version isn't
    # given its own "off" default since it's meaningless without an id.
    bedrock_guardrail_id: str
    bedrock_guardrail_version: str

    # Plan section 16's concurrency fix (concurrency.py) -- this
    # deployment runs uvicorn with its default single worker, so a
    # blocking boto3 call made directly from an async handler blocks
    # every other tenant's concurrent request on the same process.
    # global_max should not exceed thread_pool_size, or admitted
    # requests would still queue for a free thread rather than being
    # fast-rejected at the limiter.
    concurrency_global_max: int
    concurrency_default_tenant_max: int
    # Reserved-headroom priority enforcement (TenantPolicy.priority_class):
    # a "best_effort" request is additionally capped at this fraction of
    # concurrency_global_max, guaranteeing the rest is always obtainable
    # by "critical"/"standard" traffic regardless of how busy a
    # best_effort tenant is -- see concurrency.py's ConcurrencyLimiter/
    # DynamoDbConcurrencyLimiter for the actual mechanism.
    concurrency_best_effort_max_pct: float
    blocking_call_thread_pool_size: int
    # Bounds how long a request waits for a blocking call, not how long
    # the call's own thread keeps running (Python threads can't be
    # forcibly cancelled) -- see concurrency.py's BlockingCallRunner.
    blocking_call_timeout_s: float
    # Plan section 35.2 (P0 production hardening): empty means
    # in-process ConcurrencyLimiter/TokenBucketRateLimiter (correct
    # only within one ECS task -- see those classes' own docstrings).
    # Set means DynamoDbConcurrencyLimiter/DynamoDbRateLimiter, real
    # coordination across every task. One table backs both (different
    # `pk` prefixes) -- no reason to provision two.
    admission_control_table_name: str
    # Empty means no per-model AWS-quota gate at all (routing/router.py
    # falls back to breaker-only candidate skipping, same as before
    # this existed). Set means routing/model_quota.py's
    # ModelQuotaLimiter, reusing DynamoDbRateLimiter's own CAS/refill
    # logic against a dedicated table rather than admission_control_
    # table_name -- a deliberate exception to the "one table, no
    # reason to provision two" note just above: this table's rows also
    # hold rpm_limit/quota_type/updated_at, config data written by
    # scripts/sync_model_quotas_from_aws.py on its own schedule, not
    # just live request-time counters, so it doesn't fit the same
    # "purely ephemeral counters" shape admission_control_table_name's
    # rows do.
    model_quotas_table_name: str
    # A SEPARATE table from model_quotas_table_name above, deliberately
    # -- ModelQuotaLimiter's own live rate-limit counter rows
    # ("ratelimit#model#<id>", "ratelimit#model_tenant#<id>#<tenant>",
    # and their _tpm variants) used to live in model_quotas_table_name
    # alongside the out-of-band quota# config rows, on the theory that
    # one table was simpler than two. In practice that meant
    # model_quotas_table_name -- the one place an operator expects to
    # see ONLY config synced by scripts/sync_model_quotas_from_aws.py
    # -- filled up with live counter rows too, confusing at a glance
    # and mixing a table that deliberately has PITR enabled (the config
    # is worth restoring) with rows that are pure, harmless-to-lose
    # ephemeral counters (same shape as admission_control_table_name's
    # rows, which correctly have no PITR at all). Split out so
    # model_quotas_table_name is provably quota-config-only again.
    # Empty means the same as model_quotas_table_name being empty: no
    # per-model AWS-quota gate at all.
    model_ratelimits_table_name: str
    # routing/model_quota.py's per-tenant fair-share sub-cap: a single
    # tenant may never consume more than this fraction of a shared
    # model's own rpm_limit, on top of (not instead of) the overall
    # model-wide gate -- see that module's own docstring for the
    # ordering/tradeoff this implies.
    model_quota_per_tenant_share_pct: float
    # Plan section 35.18 -- how long DynamoDbConcurrencyLimiter's
    # per-request lease items live before reconcile() treats them as
    # abandoned (a crashed process, not a slow call) and compensates
    # the counters. Should stay comfortably above
    # blocking_call_timeout_s -- api/routes.py's `finally:` always
    # releases promptly even on a timeout, so a real lease should
    # never approach this ceiling under normal operation.
    concurrency_lease_ttl_s: float

    # M7 async jobs -- both empty by default (in-memory JobStore/JobQueue
    # instead of the real DynamoDB/SQS-backed ones, see main.py). Set in
    # every environment that has this repo's infra/ half's jobs queue/table
    # applied.
    jobs_queue_url: str
    jobs_table_name: str

    # M8 FinOps -- empty means in-memory UsageStore instead of the real
    # DynamoDB-backed one, same fallback pattern as jobs_table_name.
    usage_table_name: str

    # M11 Application Onboarding (provisioned-application stores) --
    # empty means the in-memory fallback (tests, or an environment that
    # hasn't applied the M11 tables yet); empty specifically also means
    # policy_store/iam_tenant_resolver stay plain
    # FilePolicyStore/FileIamTenantResolver rather than being wrapped in
    # a Layered* store (see main.py). These are read by every live
    # request (LayeredPolicyStore/LayeredIamTenantResolver's primary
    # layer), not just the admin/onboarding write surface that used to
    # populate them (moved to platform-control-plane's own backend).
    provisioned_tenant_policies_table_name: str
    provisioned_principal_mappings_table_name: str

    # Plan section 33 -- policy versioning/rollback. Empty means
    # DynamoDbPolicyStore.apply_change() still works (in-memory history
    # via InMemoryPolicyStore, or no history at all if
    # provisioned_tenant_policies_table_name is also unset) but
    # rollback()/list_history() on the Dynamo path degrade to "nothing
    # to roll back to" rather than erroring -- see store.py's
    # DynamoDbPolicyStore._archive().
    provisioned_tenant_policies_history_table_name: str

    # M12 (plan.md Section 5) -- empty means resolve AWS_IAM principals
    # in-process (LayeredIamTenantResolver, unchanged default); set
    # means delegate to platform-authz-service instead
    # (HttpIamTenantResolver). Same "seam + fallback" shape as every
    # other *_TABLE_NAME/*_URL setting in this file.
    authz_service_url: str

    # PEM-encoded CA certificate HttpIamTenantResolver pins TLS
    # verification to, since authz-service's ALB cert is issued by a
    # private CA no public trust store knows about. Empty -> use the
    # system default (fine for plain HTTP in dev/tests).
    authz_ca_cert_pem: str

    # Plan section 35 (P1 hardening) -- this service's own mTLS client
    # cert/key, presented to authz-service on every call once its ALB
    # listener's mutual_authentication is flipped to "verify" (still
    # "off" as of 2026-09-22, see HttpIamTenantResolver's own comment).
    # Both empty -> no client cert presented (today's default); set
    # together from Secrets Manager in real environments, never as a
    # plain container_env value (infra/modules/ecs_service's own
    # container_secrets).
    authz_client_cert_pem: str
    authz_client_key_pem: str


def load_settings() -> Settings:
    return Settings(
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        bedrock_model_id=os.environ.get(
            # us. prefix = cross-region inference profile ID, required by
            # newer Bedrock models instead of the bare model ID (confirmed
            # against the account this was configured for -- account
            # 646821141010, us-east-1). See docs/LOAD_TESTING.md and
            # ROADMAP.md for how to find the right ID for other models/
            # accounts (aws bedrock list-inference-profiles).
            "BEDROCK_MODEL_ID",
            "us.amazon.nova-micro-v1:0",
        ),
        bedrock_timeout_s=_env_float("BEDROCK_TIMEOUT_S", 30.0),
        bedrock_max_retries=_env_int("BEDROCK_MAX_RETRIES", 2),
        host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
        port=_env_int("GATEWAY_PORT", 8080),
        service_name=os.environ.get("SERVICE_NAME", "gateway-api"),
        service=os.environ.get("SERVICE", "bedrock-gateway-api"),
        environment=os.environ.get("ENVIRONMENT", "dev"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        max_input_chars=_env_int("MAX_INPUT_CHARS", 32_000),
        oidc_issuer=os.environ.get("OIDC_ISSUER", "https://dev-issuer.local/"),
        oidc_audience=os.environ.get("OIDC_AUDIENCE", "bedrock-gateway"),
        oidc_jwks_url=os.environ.get("OIDC_JWKS_URL", ""),
        oidc_jwks_cache_ttl_s=_env_float("OIDC_JWKS_CACHE_TTL_S", 300.0),
        dev_jwt_keypair_path=os.environ.get("DEV_JWT_KEYPAIR_PATH", ".dev/jwt_keypair.json"),
        chat_required_role=os.environ.get("CHAT_REQUIRED_ROLE", "developer"),
        iam_tenants_path=os.environ.get("IAM_TENANTS_PATH", "policies/iam_tenants.yaml"),
        enterprise_groups_path=os.environ.get("ENTERPRISE_GROUPS_PATH", ""),
        tenant_policy_path=os.environ.get("TENANT_POLICY_PATH", "policies/tenants.yaml"),
        policy_cache_ttl_s=_env_float("POLICY_CACHE_TTL_S", 30.0),
        certified_models_path=os.environ.get("CERTIFIED_MODELS_PATH", "policies/certified_models.yaml"),
        model_registry_path=os.environ.get("MODEL_REGISTRY_PATH", "policies/model_registry.yaml"),
        model_governance_fail_closed=os.environ.get("MODEL_GOVERNANCE_FAIL_CLOSED", "false").lower() == "true",
        route_set_config_path=os.environ.get("ROUTE_SET_CONFIG_PATH", "policies/route_sets.yaml"),
        response_cache_ttl_s=_env_float("RESPONSE_CACHE_TTL_S", 60.0),
        response_cache_max_entries=_env_int("RESPONSE_CACHE_MAX_ENTRIES", 1000),
        circuit_breaker_failure_threshold=_env_int("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5),
        circuit_breaker_reset_timeout_s=_env_float("CIRCUIT_BREAKER_RESET_TIMEOUT_S", 30.0),
        otel_exporter_otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        debug_capture_ttl_s=_env_float("DEBUG_CAPTURE_TTL_S", 900.0),
        audit_bucket_name=os.environ.get("AUDIT_BUCKET_NAME", ""),
        request_audit_bucket_name=os.environ.get("REQUEST_AUDIT_BUCKET_NAME", ""),
        bedrock_guardrail_id=os.environ.get("BEDROCK_GUARDRAIL_ID", ""),
        bedrock_guardrail_version=os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
        concurrency_global_max=_env_int("CONCURRENCY_GLOBAL_MAX", 32),
        concurrency_default_tenant_max=_env_int("CONCURRENCY_DEFAULT_TENANT_MAX", 8),
        concurrency_best_effort_max_pct=_env_float("CONCURRENCY_BEST_EFFORT_MAX_PCT", 0.5),
        blocking_call_thread_pool_size=_env_int("BLOCKING_CALL_THREAD_POOL_SIZE", 32),
        blocking_call_timeout_s=_env_float("BLOCKING_CALL_TIMEOUT_S", 60.0),
        admission_control_table_name=os.environ.get("ADMISSION_CONTROL_TABLE_NAME", ""),
        model_quotas_table_name=os.environ.get("MODEL_QUOTAS_TABLE_NAME", ""),
        model_ratelimits_table_name=os.environ.get("MODEL_RATELIMITS_TABLE_NAME", ""),
        model_quota_per_tenant_share_pct=_env_float("MODEL_QUOTA_PER_TENANT_SHARE_PCT", 0.4),
        concurrency_lease_ttl_s=_env_float("CONCURRENCY_LEASE_TTL_S", 300.0),
        jobs_queue_url=os.environ.get("JOBS_QUEUE_URL", ""),
        jobs_table_name=os.environ.get("JOBS_TABLE_NAME", ""),
        usage_table_name=os.environ.get("USAGE_TABLE_NAME", ""),
        provisioned_tenant_policies_table_name=os.environ.get("PROVISIONED_TENANT_POLICIES_TABLE_NAME", ""),
        provisioned_tenant_policies_history_table_name=os.environ.get(
            "PROVISIONED_TENANT_POLICIES_HISTORY_TABLE_NAME", ""
        ),
        provisioned_principal_mappings_table_name=os.environ.get(
            "PROVISIONED_PRINCIPAL_MAPPINGS_TABLE_NAME", ""
        ),
        authz_service_url=os.environ.get("AUTHZ_SERVICE_URL", ""),
        authz_ca_cert_pem=os.environ.get("AUTHZ_CA_CERT_PEM", ""),
        authz_client_cert_pem=os.environ.get("AUTHZ_CLIENT_CERT_PEM", ""),
        authz_client_key_pem=os.environ.get("AUTHZ_CLIENT_KEY_PEM", ""),
    )
