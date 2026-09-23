"""Bounded thread offload + concurrency limiting for blocking boto3
calls made from inside async route handlers (plan section 16 -- a real,
live-confirmed gap: this deployment runs uvicorn with its default
single worker, so a synchronous Bedrock/ApplyGuardrail call made
directly from `async def chat(...)` blocks the *entire* process's event
loop, starving every other tenant's concurrent request, not just the
caller's own. `TokenBucketRateLimiter`/kill-switch/ABAC all gate
*admission*; none of them protect an already-admitted request from
being starved by another tenant's in-flight blocking call).

Two parts:
  - `BlockingCallRunner` -- offloads a synchronous call to a bounded
    ThreadPoolExecutor (not asyncio's unbounded default executor) with
    a total timeout.
  - `ConcurrencyLimiter` -- a fast-reject (not queue-and-wait) counting
    semaphore, global cap and per-tenant cap, checked before a request
    is admitted to the thread pool at all. Rejecting immediately at the
    gate is what keeps a saturated thread pool from becoming its own
    unbounded queue.

A timed-out call is NOT cancelled -- Python threads can't be forcibly
killed. `timeout_s` bounds how long the *caller* waits, not how long
the orphaned thread keeps running; the thread pool's own fixed size is
what actually prevents unbounded resource growth from a pile-up of
orphaned calls, not the timeout itself. Capacity is released by the
blocking operation itself, when it actually finishes, not by the HTTP
waiter's timeout/cancellation path.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from contextlib import contextmanager
import logging
import time
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


class ConcurrencyLimiter:
    """threading.Lock, not asyncio.Lock -- acquire/release happen around
    calls that run in worker threads (via BlockingCallRunner), not just
    on the event loop thread, so this must be safe across real OS
    threads, not just concurrent coroutines."""

    def __init__(self, *, global_max: int, default_tenant_max: int):
        self._global_max = global_max
        self._default_tenant_max = default_tenant_max
        self._global_count = 0
        self._tenant_counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, tenant_id: str, *, tenant_max: Optional[int] = None) -> Optional[str]:
        """Non-blocking: returns None immediately (consuming nothing)
        rather than waiting, so a saturated limiter surfaces as a fast
        429 instead of queueing requests behind an already-slow backend.

        Returns an opaque lease token (a fresh uuid4) on success, for
        signature parity with DynamoDbConcurrencyLimiter -- a caller
        that works against either class via the same code path (see
        api/routes.py's _run_blocking_limited) can treat the result
        uniformly (truthy = admitted, pass it to release()). This
        class doesn't actually need the token itself: a crashed
        process's in-memory _tenant_counts simply vanishes with it, no
        lease-cleanup problem exists here the way it does for the
        DynamoDB-backed sibling."""
        limit = tenant_max if tenant_max is not None else self._default_tenant_max
        with self._lock:
            if self._global_count >= self._global_max:
                return None
            if self._tenant_counts.get(tenant_id, 0) >= limit:
                return None
            self._global_count += 1
            self._tenant_counts[tenant_id] = self._tenant_counts.get(tenant_id, 0) + 1
            return str(uuid.uuid4())

    def release(self, tenant_id: str, lease_token: Optional[str] = None) -> None:
        """`lease_token` is accepted but ignored -- kept only so a
        caller written against DynamoDbConcurrencyLimiter's real
        per-lease release() works unchanged against this class too."""
        with self._lock:
            if self._global_count > 0:
                self._global_count -= 1
            remaining = self._tenant_counts.get(tenant_id, 0) - 1
            if remaining > 0:
                self._tenant_counts[tenant_id] = remaining
            else:
                self._tenant_counts.pop(tenant_id, None)

    def current_global_count(self) -> int:
        with self._lock:
            return self._global_count


class DynamoDbConcurrencyLimiter:
    """Shared global/tenant counters with renewable ownership leases.

    Only a transaction deleting an existing lease may decrement counters.
    Lease expiry is application-managed: native DynamoDB TTL must not delete
    ownership records before reconciliation can compensate their counters.
    """

    _RECONCILE_PROBABILITY = 0.01

    def __init__(
        self, *, table_name: str, region: str, global_max: int, default_tenant_max: int,
        lease_ttl_s: float = 300.0, client: Optional[Any] = None,
    ):
        self._table_name = table_name
        self._global_max = global_max
        self._default_tenant_max = default_tenant_max
        self._lease_ttl_s = lease_ttl_s
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region)
        self._client = client

    def try_acquire(self, tenant_id: str, *, tenant_max: Optional[int] = None) -> Optional[str]:
        import random
        import time as time_module

        from botocore.exceptions import ClientError

        if random.random() < self._RECONCILE_PROBABILITY:
            self.reconcile()

        limit = tenant_max if tenant_max is not None else self._default_tenant_max
        if limit <= 0 or self._global_max <= 0:
            return None
        lease_id = str(uuid.uuid4())
        lease_pk = self._lease_pk(tenant_id, lease_id)
        lease_expires_at = time_module.time() + self._lease_ttl_s
        try:
            self._client.transact_write_items(
                TransactItems=[
                    self._acquire_item("concurrency#global", self._global_max),
                    self._acquire_item(f"concurrency#tenant#{tenant_id}", limit),
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": {
                                "pk": {"S": lease_pk},
                                "tenant_id": {"S": tenant_id},
                                "lease_expires_at": {"N": str(lease_expires_at)},
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
            return lease_id
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return None
            raise

    def release(self, tenant_id: str, lease_token: Optional[str] = None) -> None:
        from botocore.exceptions import ClientError

        if not lease_token:
            raise ValueError("a lease token is required to release distributed capacity")
        items = [self._release_item("concurrency#global"), self._release_item(f"concurrency#tenant#{tenant_id}")]
        if lease_token is not None:
            items.append(
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {"pk": {"S": self._lease_pk(tenant_id, lease_token)}},
                        "ConditionExpression": "attribute_exists(pk)",
                    }
                }
            )
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            # Already at zero -- a double-release (e.g. a timeout path
            # plus a subsequent finally both releasing) or crash-
            # recovery drift. Don't go negative; don't raise.
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return
            raise

    def renew(self, tenant_id: str, lease_token: str) -> None:
        self._client.update_item(
            TableName=self._table_name,
            Key={"pk": {"S": self._lease_pk(tenant_id, lease_token)}},
            UpdateExpression="SET lease_expires_at = :expiry",
            ConditionExpression="attribute_exists(pk) AND lease_expires_at > :now",
            ExpressionAttributeValues={
                ":expiry": {"N": str(time.time() + self._lease_ttl_s)},
                ":now": {"N": str(time.time())},
            },
        )

    def reconcile(self, *, now: Optional[float] = None) -> int:
        """Sweeps lease items whose `lease_expires_at` has passed and
        compensates the counters they represent -- see this class's
        own docstring for why this, not native TTL alone, is the real
        fix for the crash-leak. Returns how many stale leases were
        found and compensated. Safe to call concurrently with itself
        or with an in-flight release() of the same lease: whichever
        transaction runs first wins, the other's Delete (conditioned
        on the item still existing) cancels harmlessly.

        A full-table Scan, not a Query -- this table has no GSI, and
        lease items are naturally few and short-lived (bounded by
        global_max), so the cost is small; this runs occasionally
        (probabilistically from try_acquire), not on every request.
        """
        import time as time_module

        from botocore.exceptions import ClientError

        now = now if now is not None else time_module.time()
        swept = 0
        scan_kwargs: Dict[str, Any] = {
            "TableName": self._table_name,
            "FilterExpression": "begins_with(pk, :prefix) AND (lease_expires_at < :now OR "
                "(attribute_not_exists(lease_expires_at) AND expires_at < :now))",
            "ExpressionAttributeValues": {":prefix": {"S": "lease#"}, ":now": {"N": str(now)}},
        }
        while True:
            response = self._client.scan(**scan_kwargs)
            for item in response.get("Items", []):
                lease_pk = item["pk"]["S"]
                tenant_id = item["tenant_id"]["S"]
                try:
                    self._client.transact_write_items(
                        TransactItems=[
                            self._release_item("concurrency#global"),
                            self._release_item(f"concurrency#tenant#{tenant_id}"),
                            {
                                "Delete": {
                                    "TableName": self._table_name,
                                    "Key": {"pk": {"S": lease_pk}},
                                    "ConditionExpression": "attribute_exists(pk) AND (lease_expires_at < :now OR "
                                        "(attribute_not_exists(lease_expires_at) AND expires_at < :now))",
                                    "ExpressionAttributeValues": {":now": {"N": str(now)}},
                                }
                            },
                        ]
                    )
                    swept += 1
                except ClientError as exc:
                    # Raced with a clean release() or another
                    # reconcile() pass for the same lease -- already
                    # compensated, not an error.
                    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                        raise
            if "LastEvaluatedKey" not in response:
                break
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return swept

    def current_global_count(self) -> int:
        response = self._client.get_item(
            TableName=self._table_name, Key={"pk": {"S": "concurrency#global"}}
        )
        item = response.get("Item")
        if item is None:
            return 0
        return int(item["count_val"]["N"])

    def _lease_pk(self, tenant_id: str, lease_id: str) -> str:
        return f"lease#{tenant_id}#{lease_id}"

    def _acquire_item(self, pk: str, limit: int) -> Dict[str, Any]:
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": {"pk": {"S": pk}},
                "UpdateExpression": "ADD count_val :one",
                "ConditionExpression": "attribute_not_exists(count_val) OR count_val < :limit",
                "ExpressionAttributeValues": {":one": {"N": "1"}, ":limit": {"N": str(limit)}},
            }
        }

    def _release_item(self, pk: str) -> Dict[str, Any]:
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": {"pk": {"S": pk}},
                "UpdateExpression": "ADD count_val :neg_one",
                "ConditionExpression": "attribute_exists(count_val) AND count_val > :zero",
                "ExpressionAttributeValues": {":neg_one": {"N": "-1"}, ":zero": {"N": "0"}},
            }
        }


async def try_acquire_with_wait(
    concurrency_limiter: Any,
    tenant_id: str,
    *,
    tenant_max: Optional[int],
    max_wait_s: float,
    poll_interval_s: float = 0.1,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], Any]] = None,
) -> Optional[str]:
    """The 'Queue' state in this platform's own Admit/Queue/Reject
    admission vocabulary, applied to the synchronous /v1/chat path --
    TenantPolicy.queue_enabled opts a tenant into this instead of the
    fast-reject default (try_acquire alone). Not a real message queue
    (that's the separate, client-chosen /v1/jobs SQS path, M7): this
    polls try_acquire on the SAME request/connection, giving a slot
    that frees up mid-request a chance to admit this one instead of
    immediately 429ing.

    Bounded, not unbounded -- still gives up and returns None (the
    caller 429s exactly as it always has) once max_wait_s elapses, so a
    tenant that opts into queueing can slow other requests down for at
    most that long, never block forever behind a stuck backend.

    `clock`/`sleep` are injectable (default to time.monotonic/
    asyncio.sleep) so tests can drive this without real wall-clock
    waits -- same "injectable clock" convention as
    TokenBucketRateLimiter.
    """
    import time as time_module

    if clock is None:
        clock = time_module.monotonic
    if sleep is None:
        sleep = asyncio.sleep

    deadline = clock() + max_wait_s
    while True:
        lease = concurrency_limiter.try_acquire(tenant_id, tenant_max=tenant_max)
        if lease:
            return lease
        if clock() >= deadline:
            return None
        await sleep(poll_interval_s)


class BlockingCallCapacityError(Exception):
    pass


class BlockingCallTimeoutError(Exception):
    pass


class BlockingCallRunner:
    def __init__(self, *, max_workers: int, default_timeout_s: float):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="blocking-call")
        self._default_timeout_s = default_timeout_s
        self._slots = threading.BoundedSemaphore(max_workers)

    async def run(
        self, func: Callable[..., T], *args: Any, timeout_s: Optional[float] = None, **kwargs: Any
    ) -> T:
        if not self._slots.acquire(blocking=False):
            raise BlockingCallCapacityError("blocking executor is saturated")
        bound = partial(func, *args, **kwargs)
        try:
            future = self._executor.submit(contextvars.copy_context().run, bound)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        # Retrieve eventual errors even after the HTTP waiter has departed.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.wait_for(
                asyncio.shield(wrapped),
                timeout=timeout_s if timeout_s is not None else self._default_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            future.cancel()  # Cancel queued work only; running work retains its lease.
            raise BlockingCallTimeoutError(
                f"blocking call exceeded {timeout_s if timeout_s is not None else self._default_timeout_s}s"
            ) from exc
        except asyncio.CancelledError:
            future.cancel()
            raise


@contextmanager
def maintained_lease(limiter, tenant_id: str, token: str):
    """Renew while work runs; release only after the underlying work ends.

    Renewal failure is reported, not hidden. A network partition cannot
    forcibly cancel a remote inference; callers must still use SDK timeouts.
    """
    stopped = threading.Event()
    thread = None
    if hasattr(limiter, "renew"):
        def heartbeat():
            while not stopped.wait(max(0.01, limiter._lease_ttl_s / 3)):
                try:
                    limiter.renew(tenant_id, token)
                except Exception:
                    logging.getLogger(__name__).exception("concurrency lease renewal failed")
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
    try:
        yield
    finally:
        stopped.set()
        if thread is not None:
            thread.join()
        limiter.release(tenant_id, token)
