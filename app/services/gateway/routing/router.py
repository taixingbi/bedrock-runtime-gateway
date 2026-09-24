"""Certified model router (M4, plan section 13).

Wraps the retry-capable ConverseClient (BedrockClient already does
bounded retry + exponential backoff + jitter per call, from M0) with a
circuit breaker and fallback across a *certified* route set -- fallback
candidates always come from `policies/route_sets.yaml`, never an
arbitrary or unevaluated model. A tenant with no route_set assigned (or
one that resolves to no configured fallbacks) gets exactly one candidate
-- the model resolved by `pipeline.enforce_model_allowlist` -- so it
behaves like a direct call, M0's original behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, List, Optional, Set

from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError, ConverseClient, ConverseResult
from ..usage.token_estimate import estimate_tokens
from .circuit_breaker import CircuitBreaker
from .model_quota import ModelQuotaLimiter


@dataclass(frozen=True)
class RouteSet:
    name: str
    primary: str
    fallbacks: List[str] = field(default_factory=list)


class AllRoutesUnavailableError(Exception):
    """Every candidate model was skipped -- circuit-open, over its own
    AWS-quota budget (model_quota.ModelQuotaLimiter), or both -- no
    call was even attempted. Distinct from BedrockInvocationError,
    which means a call was attempted and failed."""

    def __init__(self, route_set_name: Optional[str]):
        super().__init__(
            f"all candidate models are unavailable (circuit-open or over quota) "
            f"for route_set={route_set_name!r}"
        )


@dataclass(frozen=True)
class RoutedResult:
    result: ConverseResult
    model_id: str
    fallback: bool


def load_route_sets_from_yaml(path: str) -> Dict[str, RouteSet]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    route_sets: Dict[str, RouteSet] = {}
    for name, cfg in (raw.get("route_sets") or {}).items():
        cfg = cfg or {}
        route_sets[name] = RouteSet(
            name=name, primary=cfg["primary"], fallbacks=list(cfg.get("fallbacks", []))
        )
    return route_sets


class CertifiedRouter:
    def __init__(
        self,
        *,
        converse_client: ConverseClient,
        circuit_breaker: CircuitBreaker,
        route_sets: Dict[str, RouteSet],
        certified_model_ids: Set[str],
        model_quota_limiter: Optional[ModelQuotaLimiter] = None,
    ):
        self.converse_client = converse_client
        self._breaker = circuit_breaker
        self._route_sets = route_sets
        self.certified_model_ids = certified_model_ids
        # Optional: None means no AWS-quota gate at all (matches every
        # other optional-infra piece in this codebase, e.g. audit_store)
        # -- every candidate is then only gated by the breaker, same as
        # before this existed.
        self._model_quota_limiter = model_quota_limiter

    def fallbacks_for(self, route_set_name: Optional[str]) -> List[str]:
        """Routing Invariant (M9): an uncertified model is filtered out
        here even if it's listed in route_sets.yaml -- being in a route
        set is necessary but not sufficient, it must also have passed
        evaluation (see certification.py)."""
        if route_set_name is None:
            return []
        route_set = self._route_sets.get(route_set_name)
        if route_set is None:
            return []
        return [m for m in route_set.fallbacks if m in self.certified_model_ids]

    def converse(
        self,
        *,
        primary_model_id: str,
        route_set_name: Optional[str],
        messages: List[BedrockChatMessage],
        max_tokens: int,
        temperature: float,
        tenant_id: Optional[str] = None,
    ) -> RoutedResult:
        """Tries primary_model_id, then the route set's fallbacks in
        order, skipping any model whose breaker is currently open,
        that's over its own AWS-quota budget or (when `tenant_id` is
        supplied) this tenant's own fair share of it
        (model_quota_limiter, if configured), or that isn't certified.
        Returns the first success. Raises the last BedrockInvocationError
        if every attempted candidate failed, or AllRoutesUnavailableError
        if every candidate was skipped -- including when primary_model_id
        itself isn't certified. Callers should
        prefer pipeline.enforce_model_certification for that specific
        case (a clean 403 before ever reaching here); this is the
        backstop for any caller that doesn't go through that stage
        (e.g. the M7 worker, which certification-checks at job
        submission, not at process time)."""
        fallbacks = [m for m in self.fallbacks_for(route_set_name) if m != primary_model_id]
        candidates = [primary_model_id, *fallbacks]
        candidates = [m for m in candidates if m in self.certified_model_ids]

        # Computed once, not per-candidate -- it depends only on this
        # request's own messages/max_tokens, same estimate for every
        # candidate model tried. None when there's no quota limiter at
        # all, so a caller with model_quota_limiter=None pays zero cost
        # for this (BedrockChatMessage has no .content attribute --
        # SimpleNamespace adapts .text to what usage/token_estimate.py
        # expects rather than changing that module's shared protocol
        # for this one caller).
        estimated_tokens = None
        if self._model_quota_limiter is not None:
            estimated_tokens = estimate_tokens(
                (SimpleNamespace(content=m.text) for m in messages), max_tokens,
            )

        last_error: Optional[BedrockInvocationError] = None
        for index, model_id in enumerate(candidates):
            if not self._breaker.allow(model_id):
                continue
            if self._model_quota_limiter is not None and not self._model_quota_limiter.allow(
                model_id, tenant_id, estimated_tokens=estimated_tokens
            ):
                continue
            try:
                result = self.converse_client.converse(
                    model_id=model_id, messages=messages, max_tokens=max_tokens, temperature=temperature
                )
            except BedrockInvocationError as exc:
                self._breaker.record_failure(model_id)
                last_error = exc
                continue
            self._breaker.record_success(model_id)
            return RoutedResult(result=result, model_id=model_id, fallback=(index > 0))

        if last_error is not None:
            raise last_error
        raise AllRoutesUnavailableError(route_set_name)
