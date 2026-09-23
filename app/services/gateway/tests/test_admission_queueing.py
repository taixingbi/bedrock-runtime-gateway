"""End-to-end coverage for TenantPolicy.queue_enabled (concurrency.py's
try_acquire_with_wait, wired into api/routes.py's _run_blocking_limited)
-- unlike test_concurrency.py's unit tests, these exercise the real
/v1/chat route through create_app()/TestClient, proving the policy flag
actually changes observable HTTP behavior, not just that the helper
function works in isolation.

Uses a real (short, ~one poll-interval) wait rather than an injected
fake clock/sleep -- _run_blocking_limited doesn't expose clock/sleep
overrides to callers (production always uses the real ones), so this
accepts a small amount of real wall-clock time instead of reaching
around the route to fake it.
"""
import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy
from ..policy.store import InMemoryPolicyStore
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient

_TENANT_ID = "queue-test-tenant"


class _EventuallyAdmitsLimiter:
    """Fails the first `fail_times` try_acquire calls, then admits --
    simulates another in-flight request's slot freeing up mid-wait."""

    def __init__(self, *, fail_times: int):
        self._remaining_failures = fail_times
        self.attempts = 0

    def try_acquire(self, tenant_id, *, tenant_max=None):
        self.attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return None
        return "lease-token"

    def release(self, tenant_id, lease_token=None):
        pass


def _client_for(policy: TenantPolicy, limiter) -> TestClient:
    settings = load_settings()
    fixture = get_auth_fixture()
    app = create_app(
        settings=settings,
        converse_client=FakeConverseClient(),
        token_verifier=fixture.verifier,
        policy_store_primary=InMemoryPolicyStore({_TENANT_ID: policy}),
        concurrency_limiter=limiter,
    )
    return TestClient(app)


def _post_chat(client: TestClient) -> "object":
    token = get_auth_fixture().token(tenant_id=_TENANT_ID)
    return client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=auth_header(token),
    )


class QueueOnRejectTests(unittest.TestCase):
    def test_queue_enabled_waits_for_a_freed_slot_instead_of_rejecting(self):
        policy = TenantPolicy(
            tenant_id=_TENANT_ID, queue_enabled=True, queue_max_wait_s=5.0, max_concurrency=1,
        )
        limiter = _EventuallyAdmitsLimiter(fail_times=1)
        client = _client_for(policy, limiter)

        resp = _post_chat(client)

        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(limiter.attempts, 2)  # first rejected, retried and admitted

    def test_queue_disabled_rejects_immediately_even_though_a_retry_would_have_succeeded(self):
        """Same limiter shape as the test above (would admit on a
        second attempt) -- with queue_enabled left at its False
        default, the fast-reject path never gets that second attempt."""
        policy = TenantPolicy(tenant_id=_TENANT_ID, max_concurrency=1)
        limiter = _EventuallyAdmitsLimiter(fail_times=1)
        client = _client_for(policy, limiter)

        resp = _post_chat(client)

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "CONCURRENCY_LIMIT_EXCEEDED")
        self.assertEqual(limiter.attempts, 1)

    def test_queue_enabled_still_gives_up_once_max_wait_elapses(self):
        policy = TenantPolicy(
            tenant_id=_TENANT_ID, queue_enabled=True, queue_max_wait_s=0.05, max_concurrency=1,
        )

        class NeverAdmitsLimiter:
            def try_acquire(self, tenant_id, *, tenant_max=None):
                return None

            def release(self, tenant_id, lease_token=None):
                pass

        client = _client_for(policy, NeverAdmitsLimiter())

        resp = _post_chat(client)

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "CONCURRENCY_LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
