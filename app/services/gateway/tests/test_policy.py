import unittest

from starlette.testclient import TestClient

from .. import pipeline
from ..auth.identity import Identity
from ..config import load_settings
from ..main import create_app
from ..policy.cache import PolicySnapshotCache
from ..policy.models import TenantPolicy, TenantState, UnknownTenantError
from ..policy.rate_limiter import TokenBucketRateLimiter
from ..policy.store import InMemoryPolicyStore
from .auth_fixtures import auth_header, get_auth_fixture
from .fake_clock import FakeClock
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class ResolvePolicyTests(unittest.TestCase):
    def test_known_tenant_resolves(self):
        store = InMemoryPolicyStore({"acme": _policy()})
        cache = PolicySnapshotCache(store=store, ttl_s=30.0)
        identity = Identity(sub="u1", tenant_id="acme", application_id="app1", roles=[])

        policy = pipeline.resolve_policy(identity, policy_cache=cache)

        self.assertEqual(policy.tenant_id, "acme")

    def test_unknown_tenant_is_pipeline_error_403(self):
        store = InMemoryPolicyStore({})
        cache = PolicySnapshotCache(store=store, ttl_s=30.0)
        identity = Identity(sub="u1", tenant_id="ghost", application_id="app1", roles=[])

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.resolve_policy(identity, policy_cache=cache)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "TENANT_NOT_PROVISIONED")


class KillSwitchTests(unittest.TestCase):
    def test_active_passes(self):
        pipeline.enforce_kill_switch(_policy(state=TenantState.ACTIVE))  # no raise

    def test_throttled_passes(self):
        pipeline.enforce_kill_switch(_policy(state=TenantState.THROTTLED))  # no raise

    def test_suspended_blocks(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_kill_switch(_policy(state=TenantState.SUSPENDED))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "TENANT_BLOCKED")

    def test_emergency_block_blocks(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_kill_switch(_policy(state=TenantState.EMERGENCY_BLOCK))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "TENANT_BLOCKED")


class RateLimitTests(unittest.TestCase):
    def test_within_budget_allowed(self):
        limiter = TokenBucketRateLimiter()
        pipeline.enforce_rate_limit(_policy(rpm_limit=60), rate_limiter=limiter)  # no raise

    def test_exceeding_budget_is_429(self):
        limiter = TokenBucketRateLimiter()
        policy = _policy(tenant_id="acme", rpm_limit=1)

        pipeline.enforce_rate_limit(policy, rate_limiter=limiter)  # consumes the only token
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_rate_limit(policy, rate_limiter=limiter)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.code, "QUOTA_EXCEEDED")

    def test_tenants_are_isolated(self):
        """Tenant A exhausting its budget must not affect tenant B (plan
        section 1's isolation invariant applied to rate limiting)."""
        limiter = TokenBucketRateLimiter()
        tenant_a = _policy(tenant_id="tenant-a", rpm_limit=1)
        tenant_b = _policy(tenant_id="tenant-b", rpm_limit=1)

        pipeline.enforce_rate_limit(tenant_a, rate_limiter=limiter)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enforce_rate_limit(tenant_a, rate_limiter=limiter)

        pipeline.enforce_rate_limit(tenant_b, rate_limiter=limiter)  # unaffected by tenant_a

    def test_throttled_state_reduces_effective_limit(self):
        limiter = TokenBucketRateLimiter()
        # rpm_limit=5 -> THROTTLED effective limit is 5 // 5 == 1
        policy = _policy(tenant_id="acme", rpm_limit=5, state=TenantState.THROTTLED)

        pipeline.enforce_rate_limit(policy, rate_limiter=limiter)  # consumes the only token
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enforce_rate_limit(policy, rate_limiter=limiter)


class TokenRateLimitTests(unittest.TestCase):
    """TenantPolicy.tpm_limit -- separate from RateLimitTests above
    (rpm_limit bounds request *rate*, not token volume)."""

    def test_no_tpm_limit_configured_is_a_noop(self):
        limiter = TokenBucketRateLimiter()
        policy = _policy(tpm_limit=None)  # default
        pipeline.enforce_token_rate_limit(policy, rate_limiter=limiter, estimated_tokens=1_000_000)  # no raise

    def test_within_budget_allowed(self):
        limiter = TokenBucketRateLimiter()
        policy = _policy(tpm_limit=1000)
        pipeline.enforce_token_rate_limit(policy, rate_limiter=limiter, estimated_tokens=500)  # no raise

    def test_exceeding_budget_is_429(self):
        limiter = TokenBucketRateLimiter()
        policy = _policy(tenant_id="acme", tpm_limit=1000)

        pipeline.enforce_token_rate_limit(policy, rate_limiter=limiter, estimated_tokens=900)
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_token_rate_limit(policy, rate_limiter=limiter, estimated_tokens=200)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.code, "TOKEN_RATE_LIMIT_EXCEEDED")

    def test_separate_bucket_from_rpm_rate_limit(self):
        """A tenant well within its RPM budget (few requests) can still
        hit its TPM budget (large requests) -- the two must be
        tracked independently, sharing the rate_limiter instance but
        not the same bucket key."""
        limiter = TokenBucketRateLimiter()
        policy = _policy(tenant_id="acme", rpm_limit=1000, tpm_limit=1000)

        pipeline.enforce_rate_limit(policy, rate_limiter=limiter)  # RPM: 1 of 1000, plenty left
        pipeline.enforce_token_rate_limit(policy, rate_limiter=limiter, estimated_tokens=999)
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_token_rate_limit(policy, rate_limiter=limiter, estimated_tokens=10)

        self.assertEqual(ctx.exception.code, "TOKEN_RATE_LIMIT_EXCEEDED")
        pipeline.enforce_rate_limit(policy, rate_limiter=limiter)  # RPM still has plenty of room

    def test_tenants_are_isolated(self):
        limiter = TokenBucketRateLimiter()
        tenant_a = _policy(tenant_id="tenant-a", tpm_limit=100)
        tenant_b = _policy(tenant_id="tenant-b", tpm_limit=100)

        pipeline.enforce_token_rate_limit(tenant_a, rate_limiter=limiter, estimated_tokens=100)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enforce_token_rate_limit(tenant_a, rate_limiter=limiter, estimated_tokens=1)

        pipeline.enforce_token_rate_limit(tenant_b, rate_limiter=limiter, estimated_tokens=100)  # unaffected


class ModelAllowlistTests(unittest.TestCase):
    def test_empty_allowlist_permits_any_requested_model(self):
        policy = _policy(models=[])
        resolved = pipeline.enforce_model_allowlist(
            policy, requested_model="anything", default_model="default-model"
        )
        self.assertEqual(resolved, "anything")

    def test_empty_allowlist_falls_back_to_default(self):
        policy = _policy(models=[])
        resolved = pipeline.enforce_model_allowlist(
            policy, requested_model=None, default_model="default-model"
        )
        self.assertEqual(resolved, "default-model")

    def test_allowlist_permits_listed_model(self):
        policy = _policy(models=["model-a", "model-b"])
        resolved = pipeline.enforce_model_allowlist(
            policy, requested_model="model-b", default_model="default-model"
        )
        self.assertEqual(resolved, "model-b")

    def test_allowlist_falls_back_to_first_entry_when_omitted(self):
        policy = _policy(models=["model-a", "model-b"])
        resolved = pipeline.enforce_model_allowlist(
            policy, requested_model=None, default_model="default-model"
        )
        self.assertEqual(resolved, "model-a")

    def test_allowlist_rejects_model_not_listed(self):
        policy = _policy(models=["model-a"])
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_allowlist(
                policy, requested_model="model-z", default_model="default-model"
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "MODEL_NOT_ALLOWED")


class PolicySnapshotCacheTests(unittest.TestCase):
    def test_hit_within_ttl_does_not_refetch(self):
        store = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        clock = FakeClock()
        cache = PolicySnapshotCache(store=store, ttl_s=30.0, clock=clock)

        first = cache.get("acme")
        store.set_state("acme", TenantState.SUSPENDED)  # mutate backing store directly
        clock.advance(5.0)  # well within the 30s TTL
        second = cache.get("acme")

        self.assertEqual(first.state, TenantState.ACTIVE)
        self.assertEqual(second.state, TenantState.ACTIVE)  # still cached, unaware of the mutation

    def test_expired_ttl_refetches(self):
        store = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        clock = FakeClock()
        cache = PolicySnapshotCache(store=store, ttl_s=30.0, clock=clock)

        cache.get("acme")
        store.set_state("acme", TenantState.SUSPENDED)
        clock.advance(31.0)  # past the TTL bound
        refreshed = cache.get("acme")

        self.assertEqual(refreshed.state, TenantState.SUSPENDED)

    def test_push_invalidation_reflects_immediately_without_waiting_for_ttl(self):
        store = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        clock = FakeClock()
        cache = PolicySnapshotCache(store=store, ttl_s=30.0, clock=clock)

        cache.get("acme")
        store.set_state("acme", TenantState.SUSPENDED)
        cache.invalidate("acme")  # push, no time advance at all
        refreshed = cache.get("acme")

        self.assertEqual(refreshed.state, TenantState.SUSPENDED)


class KillSwitchIntegrationTests(unittest.TestCase):
    """App-level: prove a blocked tenant's request never reaches Bedrock."""

    def test_suspended_tenant_never_calls_converse_client(self):
        fake = FakeConverseClient()
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = InMemoryPolicyStore(
            {"blocked-tenant": _policy(tenant_id="blocked-tenant", state=TenantState.SUSPENDED)}
        )
        app = create_app(
            settings=settings,
            converse_client=fake,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="blocked-tenant")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "TENANT_BLOCKED")
        self.assertEqual(len(fake.calls), 0)

    def test_unknown_tenant_never_calls_converse_client(self):
        fake = FakeConverseClient()
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings,
            converse_client=fake,
            token_verifier=fixture.verifier,
            policy_store=InMemoryPolicyStore({}),
        )
        client = TestClient(app)

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(fixture.token(tenant_id="never-onboarded")),
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "TENANT_NOT_PROVISIONED")
        self.assertEqual(len(fake.calls), 0)


class RateLimitIntegrationTests(unittest.TestCase):
    def test_exceeding_rpm_limit_returns_429(self):
        fake = FakeConverseClient()
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = InMemoryPolicyStore(
            {"tight-tenant": _policy(tenant_id="tight-tenant", rpm_limit=1)}
        )
        app = create_app(
            settings=settings,
            converse_client=fake,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="tight-tenant")

        first = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )
        second = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "QUOTA_EXCEEDED")


class PolicyPushInvalidationTests(unittest.TestCase):
    """The admin/onboarding HTTP surface that used to flip tenant state
    (admin_routes.py) moved to platform-control-plane's own backend --
    it calls policy_store.set_state() + policy_cache.invalidate() the
    same way that endpoint used to, exercising this app's own read path
    only (policy_store/policy_cache are shared state across both real
    services via the same DynamoDB table in production)."""

    def test_state_change_propagates_immediately_to_next_chat_request(self):
        """Push invalidation (plan section 8): the very next request after
        a state flip sees it -- no need to wait out the policy cache TTL."""
        fake = FakeConverseClient()
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme", rpm_limit=60)})
        policy_cache = PolicySnapshotCache(store=policy_store, ttl_s=settings.policy_cache_ttl_s)
        app = create_app(
            settings=settings,
            converse_client=fake,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
            policy_cache=policy_cache,
        )
        client = TestClient(app)
        chat_token = fixture.token(tenant_id="acme", roles=["developer"])

        pre = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(chat_token),
        )
        self.assertEqual(pre.status_code, 200)

        policy_store.set_state("acme", TenantState.EMERGENCY_BLOCK)
        policy_cache.invalidate("acme")

        post = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(chat_token),
        )

        self.assertEqual(post.status_code, 403)
        self.assertEqual(post.json()["error"]["code"], "TENANT_BLOCKED")
        self.assertEqual(len(fake.calls), 1)  # only the pre-block request went through


if __name__ == "__main__":
    unittest.main()
