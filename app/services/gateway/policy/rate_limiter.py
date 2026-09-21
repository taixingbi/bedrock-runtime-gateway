"""Per-tenant token-bucket rate limiter (M2's "Rate Limit" pipeline stage,
plan section 2).

Scoped strictly per tenant_id so one tenant's burst cannot consume
another's budget -- the isolation invariant (plan section 1) applied to
request capacity, not just data. This is deliberately a single-process,
in-memory bucket (no Redis) for the same reason the response cache and
policy store are in-memory for now: it's a real, swappable interface
(nothing else depends on the implementation) rather than provisioned
infra. Full TPM/dollar-budget tracking is FinOps (M8) -- this is just
requests-per-minute, the cheapest thing that gives capacity isolation now.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, tenant_id: str, *, rpm_limit: int) -> bool:
        """Returns True and consumes one token if tenant_id is within its
        rpm_limit budget; returns False (consuming nothing) otherwise."""
        capacity = float(max(rpm_limit, 0))
        refill_rate_per_s = capacity / 60.0
        now = self._clock()

        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None:
                bucket = _Bucket(tokens=capacity, last_refill=now)
                self._buckets[tenant_id] = bucket

            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_rate_per_s)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False


class DynamoDbRateLimiter:
    """Distributed counterpart to `TokenBucketRateLimiter` (plan
    section 35.2, P0 production hardening) -- same "per-process, one
    ECS task's rpm_limit isn't the platform's rpm_limit the instant
    desired_count > 1" gap `DynamoDbConcurrencyLimiter` closes for
    concurrency, applied to rate limiting. Same `allow(tenant_id, *,
    rpm_limit)` shape as `TokenBucketRateLimiter` -- a structural
    drop-in, not a formal Protocol (same reasoning as
    DynamoDbConcurrencyLimiter's own docstring).

    A token bucket's refill math needs a real read-modify-write, which
    DynamoDB's atomic ADD can't express (the refill amount depends on
    elapsed wall-clock time, not a fixed increment) -- this uses
    optimistic concurrency instead: read the bucket, compute the new
    state client-side (identical math to TokenBucketRateLimiter.allow),
    then a conditional UpdateItem keyed on the row being unchanged
    since the read (ConditionExpression on last_refill_ms), retried a
    bounded number of times on a concurrent-writer collision.

    After `max_retries` collisions, this fails CLOSED (returns False,
    i.e. reject) rather than open -- under the kind of extreme
    contention that exhausts every retry, a rate limiter's job is to
    protect the backend, so a few spurious 429s are the safer failure
    mode than silently admitting unlimited traffic.
    """

    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        clock: Callable[[], float] = time.time,
        max_retries: int = 5,
        client: Optional[Any] = None,
    ):
        self._table_name = table_name
        self._clock = clock
        self._max_retries = max_retries
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region)
        self._client = client

    def allow(self, tenant_id: str, *, rpm_limit: int) -> bool:
        capacity = float(max(rpm_limit, 0))
        refill_rate_per_s = capacity / 60.0
        pk = f"ratelimit#tenant#{tenant_id}"

        for _attempt in range(self._max_retries):
            now = self._clock()
            response = self._client.get_item(TableName=self._table_name, Key={"pk": {"S": pk}})
            item = response.get("Item")

            if item is None:
                tokens = capacity
                last_refill_ms = None  # no prior row -- condition below requires attribute_not_exists
            else:
                tokens = float(item["tokens"]["N"])
                last_refill_ms = item["last_refill_ms"]["N"]
                elapsed = max(0.0, now - float(last_refill_ms) / 1000.0)
                tokens = min(capacity, tokens + elapsed * refill_rate_per_s)

            if tokens < 1.0:
                # Still write the refilled (but not consumed) state back --
                # otherwise a request that arrives after a long idle gap
                # but finds tokens < 1 would never persist its own partial
                # refill, and a future caller recomputes from the same
                # stale last_refill_ms every time. Same CAS write, just no
                # -1.0 consumption.
                self._try_write(pk, tokens, now, last_refill_ms)
                return False

            if self._try_write(pk, tokens - 1.0, now, last_refill_ms):
                return True
            # Condition failed -- another request updated this row between
            # our read and write; retry with a fresh read.

        return False  # exhausted retries -- fail closed, see class docstring

    def _try_write(self, pk: str, tokens: float, now: float, last_refill_ms) -> bool:
        from decimal import Decimal

        from botocore.exceptions import ClientError

        item = {
            "pk": {"S": pk},
            "tokens": {"N": str(Decimal(str(tokens)))},
            "last_refill_ms": {"N": str(int(now * 1000))},
        }
        try:
            if last_refill_ms is None:
                self._client.put_item(
                    TableName=self._table_name, Item=item,
                    ConditionExpression="attribute_not_exists(pk)",
                )
            else:
                self._client.put_item(
                    TableName=self._table_name, Item=item,
                    ConditionExpression="last_refill_ms = :expected",
                    ExpressionAttributeValues={":expected": {"N": str(last_refill_ms)}},
                )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
