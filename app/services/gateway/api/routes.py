"""HTTP handlers for the gateway API: /healthz, /v1/chat.

FastAPI (APIRouter), not raw Starlette routing -- gets free OpenAPI
docs/schema (/docs, /openapi.json) at the cost of request bodies being
parsed/validated by FastAPI's dependency injection *before* the
handler body runs, rather than by hand after auth. That reordering is
deliberate and judged safe: no pipeline stage's ordering guarantee
(plan section 1's invariants) is about body-shape-vs-auth precedence,
only about safety/policy checks happening before the model is ever
called -- a client sending both a bad token and a malformed body gets
some 4xx either way, and no test exercises that specific combination.
main.py's `invalid_request_body` exception handler reformats FastAPI's
default validation-error shape back into this gateway's existing
ErrorResponse contract (400, INVALID_JSON/INVALID_REQUEST, request_id)
so callers see no difference from before the migration.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time
import uuid
from typing import Dict, Optional

from fastapi import APIRouter, Request
from opentelemetry import trace
from starlette.responses import JSONResponse, StreamingResponse

from .. import pipeline
from ..auth import aws_iam
from ..auth.aws_iam import IamTenantResolver
from ..auth.enterprise_groups import EnterpriseGroupResolver
from ..auth.jwt_verifier import TokenVerifier
from ..cache.keys import build_cache_key, normalize_messages
from ..cache.store import CachedResponse, ResponseCache
from ..concurrency import BlockingCallRunner, BlockingCallTimeoutError, BlockingCallCapacityError, ConcurrencyLimiter, maintained_lease
from ..config import Settings
from ..guardrails.client import GuardrailClient
from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError
from ..policy.cache import PolicySnapshotCache
from ..policy.rate_limiter import TokenBucketRateLimiter
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.model_quota import ModelQuotaLimiter
from ..routing.model_registry import ModelRegistryEntry
from ..routing.router import AllRoutesUnavailableError, CertifiedRouter
from ..streaming import stream_chat_response
from ..telemetry.cost import estimate_cost
from ..telemetry.debug_capture import DebugCaptureStore, S3AuditStore
from ..telemetry.logging import get_logger, log_event
from ..telemetry.otel import set_span_attributes
from ..telemetry.request_audit import RequestAuditEvent, RequestAuditStore, current_trace_id
from ..telemetry.slo import slo_breached
from ..usage.store import UsageStore, record_provider_usage, current_day, current_month
from ..usage.token_estimate import estimate_tokens
from .errors import error_response as _error
from .schemas import ChatRequest, ChatResponse, Usage

_chat_logger = get_logger("gateway.chat")

# BedrockInvocationError.code -> (http_status, public error code)
_ERROR_STATUS_MAP = {
    "ThrottlingException": (429, "UPSTREAM_THROTTLED"),
    "ServiceUnavailableException": (503, "UPSTREAM_UNAVAILABLE"),
    "ModelTimeoutException": (504, "UPSTREAM_TIMEOUT"),
    "InternalServerException": (502, "UPSTREAM_ERROR"),
}
_DEFAULT_ERROR_STATUS = (502, "UPSTREAM_ERROR")


def build_router(
    *,
    router: CertifiedRouter,
    settings: Settings,
    token_verifier: TokenVerifier,
    iam_tenant_resolver: IamTenantResolver,
    policy_cache: PolicySnapshotCache,
    rate_limiter: TokenBucketRateLimiter,
    guardrail_client: GuardrailClient,
    response_cache: ResponseCache,
    circuit_breaker: CircuitBreaker,
    tracer: trace.Tracer,
    debug_capture_store: DebugCaptureStore,
    usage_store: UsageStore,
    concurrency_limiter: ConcurrencyLimiter,
    blocking_call_runner: BlockingCallRunner,
    audit_store: Optional[S3AuditStore] = None,
    enterprise_group_resolver: Optional[EnterpriseGroupResolver] = None,
    model_registry: Optional[Dict[str, ModelRegistryEntry]] = None,
    request_audit_store: Optional[RequestAuditStore] = None,
    model_quota_limiter: Optional[ModelQuotaLimiter] = None,
) -> APIRouter:
    api_router = APIRouter()

    audit_context = ContextVar("chat_audit", default=None)

    async def _run_blocking_limited(tenant_policy, func, *args, **kwargs):
        # Acquire and release inside the executor operation. HTTP cancellation
        # cannot free capacity while the SDK call continues in that thread.
        #
        # Fast-reject only, no in-request wait for a freed slot: an earlier
        # version of this polled (queue_enabled/queue_max_wait_s) instead of
        # rejecting immediately, deliberately reverted -- blocking the
        # server-side connection during overload is the wrong direction
        # (it holds the ALB target/connection exactly when you want to free
        # it fastest), and a caller that wants its request to survive
        # momentary saturation should use the durable /v1/jobs SQS path
        # (M7), not an ad hoc in-process wait with none of SQS's durability.
        def execute():
            token = concurrency_limiter.try_acquire(
                tenant_policy.tenant_id, tenant_max=tenant_policy.max_concurrency,
                priority_class=tenant_policy.priority_class,
            )
            if not token:
                raise pipeline.PipelineError(429, "CONCURRENCY_LIMIT_EXCEEDED", "inference capacity exhausted")
            with maintained_lease(
                concurrency_limiter, tenant_policy.tenant_id, token,
                priority_class=tenant_policy.priority_class,
            ):
                return func(*args, **kwargs)
        try:
            return await blocking_call_runner.run(execute)
        except BlockingCallCapacityError as exc:
            raise pipeline.PipelineError(429, "CONCURRENCY_LIMIT_EXCEEDED", str(exc)) from exc

    def _finalize(audit, status):
        identity = audit.get("identity")
        if identity is None or audit.get("written") or request_audit_store is None:
            return
        fields = {key: value for key, value in audit.items()
                  if key not in ("identity", "written")}
        fields["status"] = status
        request_audit_store.write(RequestAuditEvent(
            tenant_id=identity.tenant_id, application_id=identity.application_id,
            principal=identity.sub, timestamp=time.time(), **fields,
        ))
        audit["written"] = True

    def _write_audit(
        *,
        request_id: str,
        identity,
        action: str,
        status: int,
        model: Optional[str] = None,
        policy_version: Optional[int] = None,
        authz_decision: Optional[str] = None,
        decision_id: Optional[str] = None,
        guardrail_version: Optional[str] = None,
        guardrail_action: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        estimated_cost: Optional[float] = None,
    ) -> None:
        audit = audit_context.get()
        if audit is not None:
            audit.update({key: value for key, value in locals().items()
                          if (key in RequestAuditEvent.__dataclass_fields__ or key == "identity") and value is not None})

    @api_router.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @api_router.post("/v1/chat", response_model=ChatResponse, response_model_exclude_none=True)
    async def chat(request: Request, chat_request: ChatRequest):
        audit = {"request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
                 "action": "chat.completion", "trace_id": current_trace_id()}
        context_token = audit_context.set(audit)
        try:
            response = await _chat(request, chat_request)
            if not isinstance(response, StreamingResponse):
                await asyncio.to_thread(_finalize, audit, response.status_code)
            return response
        except BaseException as exc:
            await asyncio.to_thread(_finalize, audit, 499 if isinstance(exc, asyncio.CancelledError) else 500)
            raise
        finally:
            audit_context.reset(context_token)

    async def _chat(request: Request, chat_request: ChatRequest):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        session_id = getattr(request.state, "session_id", "")

        with tracer.start_as_current_span("chat.request") as span:
            set_span_attributes(span, request_id=request_id, session_id=session_id or None)

            try:
                # No Identity yet -- a pre-identity failure (bad/missing
                # token, unknown IAM principal) has no tenant_id/
                # application_id/principal to write an audit event
                # about, so this stage is deliberately NOT audited.
                identity = pipeline.authenticate(
                    request.headers.get("authorization"),
                    token_verifier=token_verifier,
                    iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
                    iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
                    iam_tenant_resolver=iam_tenant_resolver,
                    request_id=request_id,
                    session_id=session_id or None,
                    enterprise_group_resolver=enterprise_group_resolver,
                )
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                return _error(exc.status_code, exc.code, str(exc), request_id)

            audit = audit_context.get()
            audit["identity"] = identity
            audit["authz_decision"] = "DENY"
            try:
                authz = pipeline.authorize(
                    identity, required_role=settings.chat_required_role, action="chat.completion"
                )
                audit.update(authz_decision="ALLOW", decision_id=authz.decision_id)
                policy = pipeline.resolve_policy(identity, policy_cache=policy_cache)
                audit["policy_version"] = policy.policy_epoch
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                # A role-check failure here IS a real authz DENY (unlike
                # admission_decision's rejections below, which happen
                # only after authz already ALLOWed) -- decision_id is
                # unavailable when pipeline.authorize() itself is what
                # raised (no Decision was ever returned), only when
                # resolve_policy() is what failed instead.
                _write_audit(
                    request_id=request_id, identity=identity, action="chat.completion",
                    status=exc.status_code, authz_decision="DENY" if exc.code == "FORBIDDEN" else "ALLOW",
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)

            # Plan section 34.6: kill-switch + rate-limit + TPM + budget
            # as one reported decision instead of separate calls --
            # concurrency (stage 4c) is acquired later, per-blocking-
            # call, not here (see admission_decision's docstring).
            admission = pipeline.admission_decision(
                policy, rate_limiter=rate_limiter, usage_store=usage_store,
                month=current_month(), day=current_day(), application_id=identity.application_id,
                estimated_tokens=estimate_tokens(chat_request.messages, chat_request.max_tokens),
            )
            if not admission.allowed:
                exc = admission.error
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                log_event(
                    _chat_logger, "INFO", "request rejected by admission control",
                    request_id=request_id, tenant_id=identity.tenant_id,
                    stage=admission.stage, code=exc.code, priority_class=policy.priority_class,
                )
                _write_audit(
                    request_id=request_id, identity=identity, action="chat.completion",
                    status=exc.status_code, policy_version=policy.policy_epoch,
                    # Authz already ALLOWed by this point (pipeline.authorize()
                    # above) -- admission control (budget/rate-limit/kill-switch)
                    # rejected this, not RBAC, so authz_decision stays ALLOW;
                    # admission.stage/exc.code (logged above) is what actually
                    # explains the rejection.
                    authz_decision="ALLOW", decision_id=authz.decision_id,
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)
            if admission.warning:
                log_event(
                    _chat_logger, "WARNING", "budget soft warning",
                    request_id=request_id, tenant_id=identity.tenant_id, warning=admission.warning,
                )

            set_span_attributes(
                span, tenant_id=identity.tenant_id, application_id=identity.application_id,
                policy_epoch=policy.policy_epoch, route_set=policy.route_set,
            )

            try:
                model_id = pipeline.enforce_model_allowlist(
                    policy, requested_model=chat_request.model, default_model=settings.bedrock_model_id
                )
                governance_warning = pipeline.enforce_model_certification(
                    model_id, certified_model_ids=router.certified_model_ids,
                    model_registry=model_registry, tenant_data_classification=policy.data_classification,
                    fail_closed=settings.model_governance_fail_closed,
                )
                if governance_warning:
                    log_event(
                        _chat_logger, "WARNING", "model governance warning",
                        request_id=request_id, tenant_id=identity.tenant_id, model=model_id,
                        warning=governance_warning,
                    )
                # Plan section 35.16: the resource/context-aware half of
                # centralized authorization -- model_id and
                # policy.data_classification only exist from this point
                # on, so this couldn't run any earlier (see
                # pipeline.enforce_resource_authorization's docstring).
                pipeline.enforce_resource_authorization(
                    identity, action="llm.invoke", resource_id=model_id,
                    context={"data_classification": policy.data_classification}
                    if policy.data_classification
                    else {},
                    iam_tenant_resolver=iam_tenant_resolver, request_id=request_id,
                    session_id=session_id or None,
                )
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                if exc.status_code == 403:
                    audit["authz_decision"] = "DENY"
                return _error(exc.status_code, exc.code, str(exc), request_id)

            audit["model"] = model_id
            set_span_attributes(span, model=model_id)

            combined_input_text = "\n".join(m.content for m in chat_request.messages)
            guardrail_start = time.perf_counter()
            try:
                await _run_blocking_limited(
                    policy,
                    pipeline.check_input_guardrail,
                    combined_input_text, policy=policy, guardrail_client=guardrail_client,
                )
            except BlockingCallTimeoutError as exc:
                guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
                set_span_attributes(span, status=504, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=504,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    guardrail_latency_ms=guardrail_ms, error=str(exc),
                )
                return _error(504, "UPSTREAM_TIMEOUT", str(exc), request_id)
            except pipeline.PipelineError as exc:
                guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
                set_span_attributes(
                    span, status=exc.status_code, error=str(exc),
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=guardrail_ms, blocked_reason=str(exc),
                )
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=exc.status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=guardrail_ms,
                    blocked_reason=str(exc), error=str(exc),
                )
                _write_audit(
                    request_id=request_id, identity=identity, action="chat.completion",
                    status=exc.status_code, model=model_id, policy_version=policy.policy_epoch,
                    authz_decision="ALLOW",  # authz allowed the request through; the guardrail blocked it
                    decision_id=authz.decision_id,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)
            input_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)

            messages = [
                BedrockChatMessage(role=m.role, text=m.content) for m in chat_request.messages
            ]

            if chat_request.stream:
                # No cache, no output guardrail, no fallback for streaming
                # -- documented simplification, see streaming.py's module
                # docstring. No debug capture either (nothing to capture
                # up front; the full output isn't known until the stream
                # ends, and by then it's already been sent to the client).
                if not circuit_breaker.allow(model_id):
                    set_span_attributes(span, status=503, error="circuit open")
                    return _error(
                        503, "UPSTREAM_UNAVAILABLE",
                        f"model '{model_id}' is temporarily unavailable (circuit open)",
                        request_id,
                    )
                # Streaming bypasses CertifiedRouter.converse() entirely
                # (no fallback candidates here), so its own quota gate
                # never runs unless checked explicitly -- same single-
                # check treatment as circuit_breaker.allow() just above.
                if model_quota_limiter is not None and not model_quota_limiter.allow(model_id, identity.tenant_id):
                    set_span_attributes(span, status=503, error="over model quota")
                    return _error(
                        503, "UPSTREAM_UNAVAILABLE",
                        f"model '{model_id}' is temporarily unavailable (over its AWS quota budget)",
                        request_id,
                    )
                # Iterator construction and consumption happen off the event loop;
                # the lease covers the entire upstream stream, including disconnect cleanup.
                invocation_id = str(uuid.uuid4())
                def stream_chunks():
                    token = concurrency_limiter.try_acquire(
                        identity.tenant_id, tenant_max=policy.max_concurrency,
                        priority_class=policy.priority_class,
                    )
                    if not token:
                        raise pipeline.PipelineError(429, "CONCURRENCY_LIMIT_EXCEEDED", "inference capacity exhausted")
                    with maintained_lease(
                        concurrency_limiter, identity.tenant_id, token, priority_class=policy.priority_class,
                    ):
                        chunks = router_converse_stream(
                            router, model_id=model_id, messages=messages,
                            max_tokens=chat_request.max_tokens, temperature=chat_request.temperature,
                        )
                        try:
                            yield from chunks
                        finally:
                            close = getattr(chunks, "close", None)
                            if close:
                                close()
                def complete_stream(final, status):
                    try:
                        if final is not None and final.input_tokens is not None and final.output_tokens is not None:
                            cost = estimate_cost(model_id, input_tokens=final.input_tokens, output_tokens=final.output_tokens)
                            record_provider_usage(usage_store, identity.tenant_id, identity.application_id,
                                                  cost, invocation_id=invocation_id)
                            audit.update(input_tokens=final.input_tokens, output_tokens=final.output_tokens,
                                         estimated_cost=cost)
                    finally:
                        _finalize(audit, status)
                chunk_iter = stream_chunks()
                set_span_attributes(span, status=200, stream=True)
                return StreamingResponse(
                    stream_chat_response(
                        chunk_iter,
                        on_complete=complete_stream,
                        model_id=model_id,
                        request_id=request_id,
                        tenant_id=identity.tenant_id,
                        circuit_breaker=circuit_breaker,
                        is_disconnected=request.is_disconnected,
                    ),
                    media_type="text/event-stream",
                    headers={"x-request-id": request_id, "cache-control": "no-cache"},
                )

            cache_key = build_cache_key(
                tenant_id=identity.tenant_id,
                application_id=identity.application_id,
                policy=policy,
                model_id=model_id,
                max_tokens=chat_request.max_tokens,
                temperature=chat_request.temperature,
                messages=normalize_messages(chat_request.messages),
            )
            cached = response_cache.get(cache_key)

            if cached is not None:
                # Application response-cache hits perform no provider inference.
                estimated_cost = 0.0
                payload_ref = None
                if policy.debug_capture_enabled:
                    debug_capture_store.capture(
                        request_id=request_id, tenant_id=identity.tenant_id,
                        input_text=combined_input_text, output_text=cached.text,
                    )
                    if audit_store is not None:
                        payload_ref = audit_store.write(
                            request_id=request_id, tenant_id=identity.tenant_id,
                            application_id=identity.application_id, model=cached.model_id,
                            input_text=combined_input_text, output_text=cached.text,
                            input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                        )
                set_span_attributes(
                    span, status=200, model=cached.model_id,
                    guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                    guardrail_latency_ms=input_guardrail_ms,
                    input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                    latency_ms=0.0, retry_count=0, fallback=False, cache_hit=True,
                    estimated_cost=estimated_cost, slo_breach=False,
                )
                log_event(
                    _chat_logger, "INFO", "chat request completed",
                    request_id=request_id,
                    tenant_id=identity.tenant_id, route_set=policy.route_set, policy_epoch=policy.policy_epoch,
                    model=cached.model_id, guardrail_version=policy.guardrail_policy,
                    guardrail_action="ALLOW", blocked_reason=None, payload_ref=payload_ref,
                    input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                    guardrail_latency_ms=input_guardrail_ms, ttft_ms=None, latency_ms=0.0,
                    retry_count=0, fallback=False, cache_hit=True,
                    estimated_cost=estimated_cost, slo_breach=False, status=200,
                )
                _write_audit(
                    request_id=request_id, identity=identity, action="chat.completion", status=200,
                    model=cached.model_id, policy_version=policy.policy_epoch, authz_decision="ALLOW",
                    decision_id=authz.decision_id,
                    guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                    input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                    estimated_cost=estimated_cost,
                )
                response = ChatResponse(
                    request_id=request_id,
                    model=cached.model_id,
                    output=cached.text,
                    stop_reason=cached.stop_reason,
                    usage=Usage(input_tokens=cached.input_tokens, output_tokens=cached.output_tokens),
                    latency_ms=0.0,
                    cache_hit=True,
                    fallback=False,
                )
                return JSONResponse(response.model_dump())

            start = time.perf_counter()
            try:
                invocation_id = str(uuid.uuid4())
                def invoke_and_account(**kwargs):
                    routed = router.converse(**kwargs)
                    result = routed.result
                    cost = estimate_cost(routed.model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens)
                    record_provider_usage(usage_store, identity.tenant_id, identity.application_id,
                                          cost, invocation_id=invocation_id)
                    return routed
                routed = await _run_blocking_limited(
                    policy,
                    invoke_and_account,
                    primary_model_id=model_id,
                    route_set_name=policy.route_set,
                    messages=messages,
                    max_tokens=chat_request.max_tokens,
                    temperature=chat_request.temperature,
                    tenant_id=identity.tenant_id,
                )
            except BedrockInvocationError as exc:
                status_code, error_code = _ERROR_STATUS_MAP.get(exc.code, _DEFAULT_ERROR_STATUS)
                set_span_attributes(span, status=status_code, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=str(exc),
                )
                return _error(status_code, error_code, str(exc), request_id)
            except AllRoutesUnavailableError as exc:
                set_span_attributes(span, status=503, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=503,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=str(exc),
                )
                return _error(503, "ALL_ROUTES_UNAVAILABLE", str(exc), request_id)
            except BlockingCallTimeoutError as exc:
                set_span_attributes(span, status=504, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=504,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=str(exc),
                )
                return _error(504, "UPSTREAM_TIMEOUT", str(exc), request_id)
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=exc.status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=str(exc),
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)

            result = routed.result
            audit.update(model=routed.model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                         estimated_cost=estimate_cost(routed.model_id, input_tokens=result.input_tokens,
                                                      output_tokens=result.output_tokens))

            guardrail_start = time.perf_counter()
            try:
                await _run_blocking_limited(
                    policy,
                    pipeline.check_output_guardrail,
                    result.text, policy=policy, guardrail_client=guardrail_client,
                )
            except BlockingCallTimeoutError as exc:
                output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
                set_span_attributes(
                    span, status=504, error=str(exc), model=routed.model_id,
                    guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                )
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=routed.model_id, status=504,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                    error=str(exc),
                )
                return _error(504, "UPSTREAM_TIMEOUT", str(exc), request_id)
            except pipeline.PipelineError as exc:
                audit.update(guardrail_action="BLOCK", guardrail_version=policy.guardrail_policy)
                output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
                set_span_attributes(
                    span, status=exc.status_code, error=str(exc), model=routed.model_id,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                    blocked_reason=str(exc),
                )
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=routed.model_id, status=exc.status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                    blocked_reason=str(exc), error=str(exc),
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)
            output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)

            response_cache.set(
                cache_key,
                CachedResponse(
                    text=result.text,
                    stop_reason=result.stop_reason,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    model_id=routed.model_id,
                ),
            )

            payload_ref = None
            if policy.debug_capture_enabled:
                debug_capture_store.capture(
                    request_id=request_id, tenant_id=identity.tenant_id,
                    input_text=combined_input_text, output_text=result.text,
                )
                if audit_store is not None:
                    payload_ref = audit_store.write(
                        request_id=request_id, tenant_id=identity.tenant_id,
                        application_id=identity.application_id, model=routed.model_id,
                        input_text=combined_input_text, output_text=result.text,
                        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                    )

            estimated_cost = estimate_cost(
                routed.model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens
            )
            breached = slo_breached(policy, result.latency_ms)

            set_span_attributes(
                span, status=200, model=routed.model_id,
                guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                latency_ms=result.latency_ms, retry_count=result.retry_count,
                fallback=routed.fallback, cache_hit=False,
                estimated_cost=estimated_cost, slo_breach=breached,
            )
            log_event(
                _chat_logger, "INFO", "chat request completed",
                request_id=request_id,
                tenant_id=identity.tenant_id,
                route_set=policy.route_set,
                policy_epoch=policy.policy_epoch,
                model=routed.model_id,
                guardrail_version=policy.guardrail_policy,
                guardrail_action="ALLOW",
                blocked_reason=None,
                payload_ref=payload_ref,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                ttft_ms=None,
                latency_ms=result.latency_ms,
                retry_count=result.retry_count,
                fallback=routed.fallback,
                cache_hit=False,
                estimated_cost=estimated_cost,
                slo_breach=breached,
                status=200,
            )
            _write_audit(
                request_id=request_id, identity=identity, action="chat.completion", status=200,
                model=routed.model_id, policy_version=policy.policy_epoch, authz_decision="ALLOW",
                decision_id=authz.decision_id,
                guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                estimated_cost=estimated_cost,
            )

            response = ChatResponse(
                request_id=request_id,
                model=routed.model_id,
                output=result.text,
                stop_reason=result.stop_reason,
                usage=Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
                latency_ms=result.latency_ms,
                cache_hit=False,
                fallback=routed.fallback,
            )
            return JSONResponse(response.model_dump())

    return api_router


def router_converse_stream(router: CertifiedRouter, *, model_id, messages, max_tokens, temperature):
    """Streaming bypasses CertifiedRouter's fallback loop (see module
    docstring) but still goes through the same underlying ConverseClient
    the router wraps, so streaming and non-streaming share one Bedrock
    client configuration."""
    return router.converse_client.converse_stream(
        model_id=model_id, messages=messages, max_tokens=max_tokens, temperature=temperature
    )
