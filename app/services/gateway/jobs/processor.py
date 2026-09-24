"""Async job execution (M7) -- the worker-side half of the request
pipeline. A pure function (no SQS client, no polling loop) so it's
unit-testable the same way pipeline.py's stages are; services/worker/
main.py is the thin adapter that actually long-polls SQS and calls this.

Submission (api/jobs_routes.py) already ran auth/authz/kill-switch/
rate-limit/model-allowlist/input-guardrail before the job record was
even written -- a rejected submission never reaches this module.
process_one() re-checks the kill switch against the *current* policy
snapshot (not the one in effect at submission time), since a tenant can
be suspended in the gap between a job being queued and a worker picking
it up (Policy Invariant, plan section 1). Nothing else the pipeline
enforces at submission is re-checked here -- rate limiting and the input
guardrail already ran once and aren't meaningful to re-run against a
queued job.
"""
from __future__ import annotations

from ..concurrency import maintained_lease
from .heartbeat import heartbeat
from .models import JobBusyError
from ..usage.store import record_provider_usage

import dataclasses
import json
import time

from .. import pipeline
from ..guardrails.client import GuardrailClient
from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError
from ..policy.cache import PolicySnapshotCache
from ..policy.models import BLOCKING_STATES
from ..routing.router import AllRoutesUnavailableError, CertifiedRouter
from ..telemetry.cost import estimate_cost
from ..telemetry.logging import get_logger, log_event
from ..telemetry.metrics import emit_request_metric
from ..usage.store import UsageStore
from .models import JobNotFoundError, JobStatus
from .store import JobStore

_logger = get_logger("gateway.worker")


def process_one(
    message_body: str,
    *,
    environment: str,
    job_store: JobStore,
    policy_cache: PolicySnapshotCache,
    guardrail_client: GuardrailClient,
    router: CertifiedRouter,
    usage_store: UsageStore,
    concurrency_limiter,
) -> None:
    job_id = json.loads(message_body)["job_id"]

    try:
        job = job_store.claim(job_id)
    except JobNotFoundError:
        log_event(_logger, "ERROR", "job record missing for queued message", job_id=job_id)
        return

    if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        # SQS is at-least-once -- a redelivered message for an already
        # SUCCEEDED/FAILED job must not re-run it.
        return

    policy = policy_cache.get(job.tenant_id)
    if policy.state in BLOCKING_STATES:
        job_store.finish(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code="TENANT_BLOCKED",
            error_message=f"tenant '{job.tenant_id}' is {policy.state.value}",
        ))
        emit_request_metric(
            environment=environment, tenant_id=job.tenant_id, reject_stage="kill_switch")
        return


    limiter = concurrency_limiter
    token = limiter.try_acquire(
        job.tenant_id, tenant_max=policy.max_concurrency, priority_class=policy.priority_class,
    )
    if not token:
        job_store.finish(dataclasses.replace(job, status=JobStatus.QUEUED))
        emit_request_metric(
            environment=environment, tenant_id=job.tenant_id, reject_stage="concurrency")
        raise JobBusyError("inference capacity exhausted")
    with maintained_lease(limiter, job.tenant_id, token, priority_class=policy.priority_class):
        with heartbeat(lambda: job_store.renew(job)):
            _execute(job, policy, job_store, guardrail_client, router, usage_store, environment)


def _execute(job, policy, job_store, guardrail_client, router, usage_store, environment):
    start = time.perf_counter()

    def _e2e_ms() -> float:
        return round((time.perf_counter() - start) * 1000, 2)

    messages = [BedrockChatMessage(role=m.role, text=m.content) for m in job.messages]
    try:
        routed = router.converse(
            primary_model_id=job.model,
            route_set_name=policy.route_set,
            messages=messages,
            max_tokens=job.max_tokens,
            temperature=job.temperature,
            tenant_id=job.tenant_id,
        )
    except BedrockInvocationError as exc:
        job_store.finish(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code=exc.code, error_message=str(exc),
        ))
        emit_request_metric(
            environment=environment, tenant_id=job.tenant_id, model=job.model, e2e_latency_ms=_e2e_ms(), error=True)
        return
    except AllRoutesUnavailableError as exc:
        job_store.finish(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code="ALL_ROUTES_UNAVAILABLE", error_message=str(exc),
        ))
        emit_request_metric(
            environment=environment, tenant_id=job.tenant_id, model=job.model, e2e_latency_ms=_e2e_ms(), error=True)
        return

    result = routed.result
    estimated_cost = estimate_cost(
        routed.model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens
    )
    record_provider_usage(usage_store, job.tenant_id, job.application_id, estimated_cost,
                          invocation_id=job.execution_id)
    try:
        pipeline.check_output_guardrail(result.text, policy=policy, guardrail_client=guardrail_client)
    except pipeline.PipelineError as exc:
        job_store.finish(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code=exc.code, error_message=str(exc),
        ))
        emit_request_metric(
            environment=environment,
            tenant_id=job.tenant_id, model=routed.model_id, e2e_latency_ms=_e2e_ms(),
            reject_stage="output_guardrail",
        )
        return

    job_store.finish(dataclasses.replace(
        job,
        status=JobStatus.SUCCEEDED,
        output=result.text,
        usage_input_tokens=result.input_tokens,
        usage_output_tokens=result.output_tokens,
    ))
    log_event(
        _logger, "INFO", "job completed",
        job_id=job.job_id, tenant_id=job.tenant_id, model=routed.model_id, status=JobStatus.SUCCEEDED.value,
    )
    emit_request_metric(
        environment=environment,
        tenant_id=job.tenant_id, model=routed.model_id, e2e_latency_ms=_e2e_ms(),
        estimated_cost_usd=estimated_cost,
    )
