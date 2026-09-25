"""Per-model requests-per-minute AND tokens-per-minute gate, protecting
against exceeding AWS Bedrock's own account-wide on-demand/cross-region
model quota (pulled from AWS Service Quotas by
scripts/sync_model_quotas_from_aws.py into gateway-model-quotas-dev's
rpm_limit/tpm_limit attributes -- never guessed or hardcoded here; see
that script's own docstring for why).

TPM is additive to RPM, never a replacement: AWS publishes both as
independent quotas (e.g. nova-micro's cross-region quota is 400 RPM
*and* 8,000,000 TPM -- a request pattern could exhaust either one
first), so both are checked, and either one failing skips the
candidate. TPM enforcement needs a per-call token estimate (unlike RPM,
where every call is worth a flat 1) -- router.py computes this the same
way pipeline.py's tenant-level TPM stage does (usage/token_estimate.py)
and passes it in as `estimated_tokens`; a caller that doesn't pass one
(or a model with no synced tpm_limit) simply skips the TPM gate, same
fail-open convention as an unsynced rpm_limit.

Wired into routing/router.py's per-candidate loop, the same place the
circuit breaker gates a candidate -- an exhausted model's own quota is
treated exactly like an open breaker: skip to the next candidate in
the route set, not a bespoke new pipeline.py admission stage (per-
model routing is router.py's job; pipeline.py's stages are all
tenant-scoped). Rerouting before a call is attempted is a real, if
secondary, improvement over the existing reactive fallback-on-
ThrottlingException path (router.py's own docstring): it avoids the
wasted round-trip/latency of a call Bedrock would throttle anyway --
it does not replace that reactive path, which still covers any drift
between what this gate believes the quota is and Bedrock's live truth.

Two independently swappable pieces, deliberately reading/writing two
SEPARATE tables (not two rows in one table -- see config.py's own note
on why gateway-model-quotas-dev and gateway-model-ratelimits-dev were
split apart):
  - ModelQuotaCache -- reads gateway-model-quotas-dev's rpm_limit from
    the "quota#<model_id>" row (config: rpm_limit/quota_type/
    updated_at, written only by the sync script), TTL-cached (default
    60s) so the hot request path never pays an extra DynamoDB read for
    a value that only changes on the order of "whenever someone
    re-runs the sync script", not per request.
  - ModelQuotaLimiter -- wraps a DynamoDbRateLimiter instance (the
    exact same CAS token-bucket class tenants use for rate limiting,
    policy/rate_limiter.py, pointed at gateway-model-ratelimits-dev via
    key_prefix="ratelimit#model#") with the cache above, so the only
    thing routing/router.py calls is allow(model_id) -> bool. Living in
    a table the sync script never touches at all (rather than merely a
    differently-prefixed row in the same table) means there's no longer
    even a theoretical chance of a counter row colliding with a config
    write -- gateway-model-quotas-dev is now provably quota-config-only.

Fails OPEN, not closed, on an unknown model (no synced row): a missing
AWS-quota row is a data-freshness gap (the sync script never ran, or
never ran for this model), not evidence the model is over capacity --
failing closed would make CertifiedRouter treat "never synced" the
same as "provably over quota", permanently skipping any model this
was never wired up for. That would make an operational gap (forgot to
run the sync script) silently worse than not having this feature at
all.

Per-tenant fair share (optional, `tenant_limiter`): the overall-model
check above is a SHARED account-wide resource -- protecting Bedrock's
own quota from the platform, not protecting any one tenant's fair
share of it from a busy sibling. A third row per (model_id, tenant_id)
pair, "ratelimit#model_tenant#<model_id>#<tenant_id>", caps any single
tenant at `per_tenant_share_pct` of the model's own rpm_limit --
checked AFTER the overall-model check (only once that passes), so a
model that's already globally exhausted rejects before ever touching a
tenant's own share budget. The reverse order-of-operations tradeoff is
real and accepted: if the model-wide check passes but a tenant's own
share is exhausted, that one overall-bucket token is "wasted" (already
consumed, request still rejected) -- judged better than the
alternative of charging a tenant's fairness budget for a request that
was going to be globally rejected regardless. This is a heuristic
fairness cap, not a guarantee under adversarial timing -- a busy
tenant can still consume its full share before a quieter one gets a
turn; it just can no longer consume the OTHER tenants' shares too.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..policy.rate_limiter import DynamoDbRateLimiter

_QUOTA_KEY_PREFIX = "quota#"


class ModelQuotaCache:
    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        client: Optional[Any] = None,
    ):
        self._table_name = table_name
        self._ttl_s = ttl_s
        self._clock = clock
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region)
        self._client = client
        # (timestamp, rpm_limit, tpm_limit) -- both limits come from
        # the same row/read, cached together so tpm_limit_for doesn't
        # cost a second DynamoDB read on top of rpm_limit_for.
        self._cache: Dict[str, Tuple[float, Optional[int], Optional[int]]] = {}
        self._lock = threading.Lock()

    def rpm_limit_for(self, model_id: str) -> Optional[int]:
        """None means "no synced quota for this model" -- callers must
        treat that as unknown, not zero (see this module's own
        fail-open docstring)."""
        return self._quota_for(model_id)[0]

    def tpm_limit_for(self, model_id: str) -> Optional[int]:
        """None means "no synced TPM quota for this model" -- same
        fail-open convention as rpm_limit_for. Distinct from "this
        model has no TPM quota concept at all" (AWS publishes one for
        every model this gate cares about; None here just means the
        sync script hasn't captured it yet, or was run before TPM
        support existed)."""
        return self._quota_for(model_id)[1]

    def _quota_for(self, model_id: str) -> Tuple[Optional[int], Optional[int]]:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(model_id)
            if cached is not None and (now - cached[0]) < self._ttl_s:
                return cached[1], cached[2]

        response = self._client.get_item(
            TableName=self._table_name, Key={"pk": {"S": f"{_QUOTA_KEY_PREFIX}{model_id}"}}
        )
        item = response.get("Item")
        rpm_limit = int(item["rpm_limit"]["N"]) if item and "rpm_limit" in item else None
        tpm_limit = int(item["tpm_limit"]["N"]) if item and "tpm_limit" in item else None

        with self._lock:
            self._cache[model_id] = (now, rpm_limit, tpm_limit)
        return rpm_limit, tpm_limit


class ModelQuotaLimiter:
    def __init__(
        self,
        *,
        cache: ModelQuotaCache,
        limiter: DynamoDbRateLimiter,
        tenant_limiter: Optional[DynamoDbRateLimiter] = None,
        per_tenant_share_pct: float = 0.4,
        tpm_limiter: Optional[DynamoDbRateLimiter] = None,
        tenant_tpm_limiter: Optional[DynamoDbRateLimiter] = None,
    ):
        self._cache = cache
        self._limiter = limiter
        # Optional: None (default) means no per-tenant fair-share gate
        # at all -- every existing caller of allow(model_id) without a
        # tenant_id behaves exactly as before this existed.
        self._tenant_limiter = tenant_limiter
        self._per_tenant_share_pct = per_tenant_share_pct
        # Optional, additive to the RPM gate above: None (default)
        # means no TPM gate at all -- a ModelQuotaLimiter built before
        # TPM support existed, or one wired without a tpm_limiter,
        # behaves exactly as it did before this existed.
        self._tpm_limiter = tpm_limiter
        self._tenant_tpm_limiter = tenant_tpm_limiter

    def allow(self, model_id: str, tenant_id: Optional[str] = None, *, estimated_tokens: Optional[int] = None) -> bool:
        rpm_limit = self._cache.rpm_limit_for(model_id)
        if rpm_limit is not None:
            if not self._limiter.allow(model_id, rpm_limit=rpm_limit):
                return False
            if tenant_id is not None and self._tenant_limiter is not None:
                per_tenant_limit = max(1, int(rpm_limit * self._per_tenant_share_pct))
                tenant_key = f"{model_id}#{tenant_id}"
                if not self._tenant_limiter.allow(tenant_key, rpm_limit=per_tenant_limit):
                    return False

        if self._tpm_limiter is not None and estimated_tokens is not None:
            tpm_limit = self._cache.tpm_limit_for(model_id)
            if tpm_limit is not None:
                if not self._tpm_limiter.allow(model_id, rpm_limit=tpm_limit, amount=estimated_tokens):
                    return False
                if tenant_id is not None and self._tenant_tpm_limiter is not None:
                    per_tenant_tpm = max(1, int(tpm_limit * self._per_tenant_share_pct))
                    tenant_key = f"{model_id}#{tenant_id}"
                    if not self._tenant_tpm_limiter.allow(tenant_key, rpm_limit=per_tenant_tpm, amount=estimated_tokens):
                        return False

        return True
