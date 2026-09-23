import asyncio
import time
import unittest

from ..concurrency import (
    BlockingCallRunner,
    BlockingCallTimeoutError,
    ConcurrencyLimiter,
    try_acquire_with_wait,
)


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


class TryAcquireWithWaitTests(unittest.IsolatedAsyncioTestCase):
    """`try_acquire_with_wait` is the 'Queue' state (TenantPolicy.
    queue_enabled) -- these drive it entirely through its injectable
    `clock`/`sleep` params so the tests run instantly, with no real
    asyncio.sleep/wall-clock waiting."""

    async def test_returns_lease_immediately_when_a_slot_is_free(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=5)
        lease = await try_acquire_with_wait(limiter, "acme", tenant_max=None, max_wait_s=5.0)
        self.assertIsInstance(lease, str)

    async def test_retries_until_a_slot_frees_and_returns_the_new_lease(self):
        class FlakyLimiter:
            def __init__(self):
                self.attempts = 0
                self.tenant_maxes_seen = []

            def try_acquire(self, tenant_id, *, tenant_max=None):
                self.attempts += 1
                self.tenant_maxes_seen.append(tenant_max)
                return None if self.attempts < 3 else "lease-3"

        clock_state = {"t": 0.0}
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            clock_state["t"] += seconds

        limiter = FlakyLimiter()
        lease = await try_acquire_with_wait(
            limiter, "acme", tenant_max=7, max_wait_s=5.0, poll_interval_s=0.1,
            clock=lambda: clock_state["t"], sleep=fake_sleep,
        )
        self.assertEqual(lease, "lease-3")
        self.assertEqual(limiter.attempts, 3)
        self.assertEqual(sleep_calls, [0.1, 0.1])
        # tenant_max must be forwarded through on every retry, not just
        # the first attempt.
        self.assertEqual(limiter.tenant_maxes_seen, [7, 7, 7])

    async def test_gives_up_and_returns_none_once_max_wait_elapses(self):
        class NeverAdmits:
            def try_acquire(self, tenant_id, *, tenant_max=None):
                return None

        clock_state = {"t": 0.0}
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            clock_state["t"] += seconds

        lease = await try_acquire_with_wait(
            NeverAdmits(), "acme", tenant_max=None, max_wait_s=0.25, poll_interval_s=0.1,
            clock=lambda: clock_state["t"], sleep=fake_sleep,
        )
        self.assertIsNone(lease)
        # Never actually slept in real time -- the fake clock advanced
        # past the deadline purely from the injected sleep's own
        # bookkeeping, proving this test didn't take 0.25s of wall time.
        self.assertGreaterEqual(clock_state["t"], 0.25)


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
