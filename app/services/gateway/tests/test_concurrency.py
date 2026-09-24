import asyncio
import time
import unittest

from ..concurrency import BlockingCallRunner, BlockingCallTimeoutError, ConcurrencyLimiter


class ConcurrencyLimiterTests(unittest.TestCase):
    def test_acquire_succeeds_under_limit(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=5)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertEqual(limiter.current_global_count(), 1)

    def test_acquire_fails_at_tenant_limit(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=2)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("acme"))  # 3rd exceeds tenant_max=2
        self.assertEqual(limiter.current_global_count(), 2)

    def test_other_tenant_unaffected_by_one_tenant_at_its_limit(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=1)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("acme"))
        self.assertTrue(limiter.try_acquire("other"))  # different tenant, own budget

    def test_acquire_fails_at_global_limit_even_under_tenant_limit(self):
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=10)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("other"))  # global cap, not tenant cap

    def test_release_frees_a_slot(self):
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=1)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("acme"))
        limiter.release("acme")
        self.assertTrue(limiter.try_acquire("acme"))

    def test_release_never_goes_negative(self):
        limiter = ConcurrencyLimiter(global_max=5, default_tenant_max=5)
        limiter.release("never-acquired")  # must not raise or underflow
        self.assertEqual(limiter.current_global_count(), 0)

    def test_per_tenant_override_max(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=1)
        self.assertTrue(limiter.try_acquire("finance", tenant_max=3))
        self.assertTrue(limiter.try_acquire("finance", tenant_max=3))
        self.assertTrue(limiter.try_acquire("finance", tenant_max=3))
        self.assertFalse(limiter.try_acquire("finance", tenant_max=3))

    def test_successful_acquire_returns_a_token_not_bare_true(self):
        """Signature parity with DynamoDbConcurrencyLimiter (plan
        section 35.18) -- a caller that works against either class via
        the same code path (api/routes.py's _run_blocking_limited)
        gets a real opaque token here too, even though this class
        doesn't need it for its own release()."""
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=5)
        token = limiter.try_acquire("acme")
        self.assertIsInstance(token, str)
        self.assertTrue(token)

    def test_release_accepts_and_ignores_a_lease_token(self):
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=1)
        token = limiter.try_acquire("acme")
        limiter.release("acme", token)
        self.assertTrue(limiter.try_acquire("acme"))


class ConcurrencyLimiterPriorityTests(unittest.TestCase):
    """Reserved-headroom priority enforcement (TenantPolicy.priority_class):
    a "best_effort" request is additionally capped at best_effort_max_pct
    of global_max, guaranteeing the rest is always obtainable by
    "critical"/"standard" traffic regardless of how busy a best_effort
    tenant is."""

    def test_default_priority_class_is_unaffected_by_the_reserved_cap(self):
        """priority_class defaults to "standard" everywhere -- no
        behavior change for any existing caller that never passes it."""
        limiter = ConcurrencyLimiter(global_max=2, default_tenant_max=10, best_effort_max_pct=0.5)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertTrue(limiter.try_acquire("acme"))  # both standard, unrestricted by best_effort cap
        self.assertFalse(limiter.try_acquire("acme"))  # global cap still applies

    def test_best_effort_is_capped_below_global_even_with_room_in_global(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=10, best_effort_max_pct=0.5)
        for _ in range(5):
            self.assertTrue(limiter.try_acquire("busy-tenant", priority_class="best_effort"))
        # global_max=10 has room, but best_effort_max=5 is exhausted.
        self.assertFalse(limiter.try_acquire("busy-tenant", priority_class="best_effort"))

    def test_standard_traffic_always_gets_its_guaranteed_headroom(self):
        """The actual noisy-neighbor guarantee: a busy best_effort
        tenant exhausting its own reserved share can never prevent
        standard traffic from using the rest of global_max."""
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=10, best_effort_max_pct=0.5)
        for _ in range(5):
            self.assertTrue(limiter.try_acquire("busy-best-effort", priority_class="best_effort"))
        self.assertFalse(limiter.try_acquire("busy-best-effort", priority_class="best_effort"))

        # Standard traffic is untouched by best_effort's own sub-cap --
        # it can still use the remaining half of the global pool.
        for _ in range(5):
            self.assertTrue(limiter.try_acquire("critical-tenant", priority_class="standard"))
        self.assertFalse(limiter.try_acquire("critical-tenant", priority_class="standard"))  # global now exhausted

    def test_release_frees_the_best_effort_reservation(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=10, best_effort_max_pct=0.1)  # cap=1
        token = limiter.try_acquire("acme", priority_class="best_effort")
        self.assertTrue(token)
        self.assertFalse(limiter.try_acquire("other", priority_class="best_effort"))  # cap of 1 exhausted

        limiter.release("acme", token, priority_class="best_effort")

        self.assertTrue(limiter.try_acquire("other", priority_class="best_effort"))  # freed

    def test_release_with_wrong_priority_class_leaks_the_reservation(self):
        """Documents the real contract, not a defect: release() must be
        called with the SAME priority_class try_acquire() used --
        maintained_lease does this automatically in production. Calling
        it with the wrong value here deliberately leaves the
        best_effort counter stuck, to prove the contract matters."""
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=10, best_effort_max_pct=0.1)
        token = limiter.try_acquire("acme", priority_class="best_effort")
        limiter.release("acme", token, priority_class="standard")  # wrong on purpose

        self.assertFalse(limiter.try_acquire("other", priority_class="best_effort"))  # still "held"


class BlockingCallRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_sync_function_and_returns_result(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)
        result = await runner.run(lambda x, y: x + y, 2, 3)
        self.assertEqual(result, 5)

    async def test_passes_kwargs_through(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)

        def f(*, a, b):
            return a * b

        result = await runner.run(f, a=4, b=5)
        self.assertEqual(result, 20)

    async def test_does_not_block_the_event_loop(self):
        """The actual bug this module fixes: a slow synchronous call must
        not prevent other coroutines from making progress concurrently."""
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)
        progressed = []

        async def ticker():
            for i in range(5):
                await asyncio.sleep(0.01)
                progressed.append(i)

        async def slow_blocking_call():
            return await runner.run(time.sleep, 0.1)

        await asyncio.gather(ticker(), slow_blocking_call())
        # If the blocking call had run on the event loop directly, the
        # ticker couldn't have advanced during those 100ms.
        self.assertEqual(progressed, [0, 1, 2, 3, 4])

    async def test_raises_blocking_call_timeout_error_on_timeout(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)
        with self.assertRaises(BlockingCallTimeoutError):
            await runner.run(time.sleep, 0.2, timeout_s=0.01)

    async def test_propagates_the_original_exception_on_success_path(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)

        def boom():
            raise ValueError("real failure")

        with self.assertRaises(ValueError):
            await runner.run(boom)


if __name__ == "__main__":
    unittest.main()
