"""Runtime dependencies shared by HTTP handlers and durable job workers."""
from .concurrency import ConcurrencyLimiter, DynamoDbConcurrencyLimiter
from .guardrails.basic_guardrail import BasicGuardrailClient
from .guardrails.bedrock_guardrail import BedrockGuardrailClient
from .policy.store import DynamoDbPolicyStore, FilePolicyStore, LayeredPolicyStore


def build_policy_store(settings, primary=None):
    fallback = FilePolicyStore(settings.tenant_policy_path)
    if primary is None and not settings.provisioned_tenant_policies_table_name:
        return fallback
    return LayeredPolicyStore(
        primary=primary if primary is not None else DynamoDbPolicyStore(
            table_name=settings.provisioned_tenant_policies_table_name,
            region=settings.aws_region,
        ), fallback=fallback,
    )


def build_guardrail_client(settings):
    if settings.bedrock_guardrail_id:
        return BedrockGuardrailClient(
            guardrail_id=settings.bedrock_guardrail_id,
            guardrail_version=settings.bedrock_guardrail_version,
            region=settings.aws_region,
        )
    return BasicGuardrailClient()


def build_concurrency_limiter(settings):
    limits = dict(global_max=settings.concurrency_global_max,
                  default_tenant_max=settings.concurrency_default_tenant_max,
                  best_effort_max_pct=settings.concurrency_best_effort_max_pct)
    if settings.admission_control_table_name:
        return DynamoDbConcurrencyLimiter(
            table_name=settings.admission_control_table_name, region=settings.aws_region,
            lease_ttl_s=settings.concurrency_lease_ttl_s, **limits,
        )
    return ConcurrencyLimiter(**limits)
