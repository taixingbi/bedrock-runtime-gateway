"""Per-model requests-per-minute gate, protecting against exceeding
AWS Bedrock's own account-wide on-demand/cross-region model quota
(pulled from AWS Service Quotas by scripts/sync_model_quotas_from_aws.py
into gateway-model-quotas-dev's rpm_limit attribute -- never guessed or
hardcoded here; see that script's own docstring for why).

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
different rows per model_id in the same table rather than one shared
row:
  - ModelQuotaCache -- reads gateway-model-quotas-dev's rpm_limit from
    the "quota#<model_id>" row (config: rpm_limit/quota_type/
    updated_at, written only by the sync script), TTL-cached (default
    60s) so the hot request path never pays an extra DynamoDB read for
    a value that only changes on the order of "whenever someone
    re-runs the sync script", not per request.
  - ModelQuotaLimiter -- wraps a DynamoDbRateLimiter instance (the
    exact same CAS token-bucket class tenants use for rate limiting,
    policy/rate_limiter.py, pointed at this table via
    key_prefix="ratelimit#model#") with the cache above, so the only
    thing routing/router.py calls is allow(model_id) -> bool.
    DynamoDbRateLimiter.allow() treats "item exists" as "this bucket
    has tokens/last_refill_ms already" -- sharing its row with the
    sync script's config write would violate that the first time a
    model's quota is synced before its bucket is ever touched, so its
    "ratelimit#model#<model_id>" row is counter-only, never written by
    anything except DynamoDbRateLimiter itself, exactly like every
    tenant's own row.

Fails OPEN, not closed, on an unknown model (no synced row): a missing
AWS-quota row is a data-freshness gap (the sync script never ran, or
never ran for this model), not evidence the model is over capacity --
failing closed would make CertifiedRouter treat "never synced" the
same as "provably over quota", permanently skipping any model this
was never wired up for. That would make an operational gap (forgot to
run the sync script) silently worse than not having this feature at
all.
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
        self._cache: Dict[str, Tuple[float, Optional[int]]] = {}
        self._lock = threading.Lock()

    def rpm_limit_for(self, model_id: str) -> Optional[int]:
        """None means "no synced quota for this model" -- callers must
        treat that as unknown, not zero (see this module's own
        fail-open docstring)."""
        now = self._clock()
        with self._lock:
            cached = self._cache.get(model_id)
            if cached is not None and (now - cached[0]) < self._ttl_s:
                return cached[1]

        response = self._client.get_item(
            TableName=self._table_name, Key={"pk": {"S": f"{_QUOTA_KEY_PREFIX}{model_id}"}}
        )
        item = response.get("Item")
        rpm_limit = int(item["rpm_limit"]["N"]) if item and "rpm_limit" in item else None

        with self._lock:
            self._cache[model_id] = (now, rpm_limit)
        return rpm_limit


class ModelQuotaLimiter:
    def __init__(self, *, cache: ModelQuotaCache, limiter: DynamoDbRateLimiter):
        self._cache = cache
        self._limiter = limiter

    def allow(self, model_id: str) -> bool:
        rpm_limit = self._cache.rpm_limit_for(model_id)
        if rpm_limit is None:
            return True  # fail open -- see module docstring
        return self._limiter.allow(model_id, rpm_limit=rpm_limit)
