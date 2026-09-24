"""routing/model_quota.py -- ModelQuotaCache/ModelQuotaLimiter, the
per-model AWS-quota gate. Real DynamoDB emulation via moto, same
reasoning as test_distributed_rate_limiter.py: this module's whole
point is reading gateway-model-quotas-dev's rpm_limit correctly and
enforcing it via DynamoDbRateLimiter's own proven CAS logic, not
worth re-testing with a hand-written fake.
"""
import unittest

import boto3
from moto import mock_aws

from ..policy.rate_limiter import DynamoDbRateLimiter
from ..routing.model_quota import ModelQuotaCache, ModelQuotaLimiter

TABLE_NAME = "test-model-quotas"
REGION = "us-east-1"


def _create_table(client) -> None:
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _put_quota(client, model_id: str, rpm_limit: int) -> None:
    client.put_item(
        TableName=TABLE_NAME,
        Item={
            "pk": {"S": f"quota#{model_id}"},
            "rpm_limit": {"N": str(rpm_limit)},
            "quota_type": {"S": "cross_region"},
        },
    )


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@mock_aws
class ModelQuotaCacheTests(unittest.TestCase):
    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.clock = FakeClock()

    def test_unknown_model_returns_none(self):
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock)
        self.assertIsNone(cache.rpm_limit_for("no-such-model"))

    def test_synced_model_returns_its_rpm_limit(self):
        _put_quota(self._client, "model-a", 50)
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock)

        self.assertEqual(cache.rpm_limit_for("model-a"), 50)

    def test_result_is_cached_within_ttl(self):
        """Changing the underlying row within the TTL window must not
        be observed -- proves the hot path isn't paying a DynamoDB
        read on every single call."""
        _put_quota(self._client, "model-a", 50)
        cache = ModelQuotaCache(
            table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock, ttl_s=60.0,
        )
        self.assertEqual(cache.rpm_limit_for("model-a"), 50)

        _put_quota(self._client, "model-a", 999)  # simulates a re-sync landing mid-TTL
        self.clock.advance(30.0)  # still within the 60s TTL

        self.assertEqual(cache.rpm_limit_for("model-a"), 50)  # stale cached value, as intended

    def test_refreshes_after_ttl_expires(self):
        _put_quota(self._client, "model-a", 50)
        cache = ModelQuotaCache(
            table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock, ttl_s=60.0,
        )
        self.assertEqual(cache.rpm_limit_for("model-a"), 50)

        _put_quota(self._client, "model-a", 999)
        self.clock.advance(61.0)  # past the TTL

        self.assertEqual(cache.rpm_limit_for("model-a"), 999)

    def test_models_are_cached_independently(self):
        _put_quota(self._client, "model-a", 50)
        _put_quota(self._client, "model-b", 1000)
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock)

        self.assertEqual(cache.rpm_limit_for("model-a"), 50)
        self.assertEqual(cache.rpm_limit_for("model-b"), 1000)


@mock_aws
class ModelQuotaLimiterTests(unittest.TestCase):
    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.clock = FakeClock()

    def _limiter(self) -> ModelQuotaLimiter:
        return ModelQuotaLimiter(
            cache=ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock),
            limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model#",
            ),
        )

    def test_unknown_model_fails_open(self):
        """No synced quota row means 'never synced', not 'over
        quota' -- see model_quota.py's own docstring for why this
        must not fail closed."""
        limiter = self._limiter()

        self.assertTrue(limiter.allow("never-synced-model"))
        self.assertTrue(limiter.allow("never-synced-model"))  # repeatedly, no budget to exhaust

    def test_synced_model_enforces_its_rpm_limit(self):
        _put_quota(self._client, "model-a", 1)
        limiter = self._limiter()

        self.assertTrue(limiter.allow("model-a"))
        self.assertFalse(limiter.allow("model-a"))

    def test_models_have_independent_budgets(self):
        _put_quota(self._client, "model-a", 1)
        _put_quota(self._client, "model-b", 1)
        limiter = self._limiter()

        self.assertTrue(limiter.allow("model-a"))
        self.assertFalse(limiter.allow("model-a"))
        self.assertTrue(limiter.allow("model-b"))  # unaffected by model-a's exhaustion


@mock_aws
class ModelQuotaLimiterFairShareTests(unittest.TestCase):
    """The per-tenant fair-share sub-cap (tenant_limiter/
    per_tenant_share_pct) -- protects a shared model's own AWS quota
    from being monopolized by one busy tenant, on top of (not instead
    of) the overall-model gate ModelQuotaLimiterTests above covers."""

    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.clock = FakeClock()

    def _limiter(self, *, per_tenant_share_pct: float = 0.4) -> ModelQuotaLimiter:
        return ModelQuotaLimiter(
            cache=ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock),
            limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model#",
            ),
            tenant_limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model_tenant#",
            ),
            per_tenant_share_pct=per_tenant_share_pct,
        )

    def test_no_tenant_id_skips_the_fair_share_check_entirely(self):
        """Backward compatible: a caller that never passes tenant_id
        (e.g. an older call site) behaves exactly like
        ModelQuotaLimiterTests -- overall-model gate only."""
        _put_quota(self._client, "model-a", 10)
        limiter = self._limiter()

        for _ in range(10):
            self.assertTrue(limiter.allow("model-a"))  # no tenant_id at all
        self.assertFalse(limiter.allow("model-a"))

    def test_no_tenant_limiter_configured_skips_the_fair_share_check(self):
        """Backward compatible the other way: tenant_id is passed, but
        this ModelQuotaLimiter instance has no tenant_limiter wired up
        (matches settings.model_quotas_table_name being set but an
        older construction site)."""
        _put_quota(self._client, "model-a", 10)
        limiter = ModelQuotaLimiter(
            cache=ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock),
            limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model#",
            ),
        )

        for _ in range(10):
            self.assertTrue(limiter.allow("model-a", "busy-tenant"))

    def test_single_tenant_is_capped_below_the_models_full_budget(self):
        _put_quota(self._client, "model-a", 10)  # per-tenant share: 10 * 0.4 = 4
        limiter = self._limiter(per_tenant_share_pct=0.4)

        for _ in range(4):
            self.assertTrue(limiter.allow("model-a", "busy-tenant"))
        # Model-wide budget (10) still has 6 left, but this tenant's
        # own 40% share (4) is exhausted.
        self.assertFalse(limiter.allow("model-a", "busy-tenant"))

    def test_a_different_tenant_is_unaffected_by_one_tenants_exhausted_share(self):
        _put_quota(self._client, "model-a", 10)
        limiter = self._limiter(per_tenant_share_pct=0.4)

        for _ in range(4):
            self.assertTrue(limiter.allow("model-a", "busy-tenant"))
        self.assertFalse(limiter.allow("model-a", "busy-tenant"))

        self.assertTrue(limiter.allow("model-a", "quiet-tenant"))  # its own, separate share

    def test_overall_model_exhaustion_rejects_even_a_tenant_under_its_own_share(self):
        _put_quota(self._client, "model-a", 2)  # tiny overall budget
        limiter = self._limiter(per_tenant_share_pct=0.9)  # generous per-tenant share (1.8 -> 1)

        self.assertTrue(limiter.allow("model-a", "tenant-a"))
        self.assertTrue(limiter.allow("model-a", "tenant-b"))
        # Overall model budget (2) is now exhausted -- rejected even
        # though neither tenant is anywhere near its own share.
        self.assertFalse(limiter.allow("model-a", "tenant-a"))

    def test_per_tenant_share_is_never_less_than_one(self):
        """A tiny model quota (e.g. 1 rpm) times a small share_pct
        would floor to 0, which would mean a tenant can NEVER be
        admitted even with a free model budget -- max(1, ...) prevents
        that degenerate case."""
        _put_quota(self._client, "model-a", 1)
        limiter = self._limiter(per_tenant_share_pct=0.1)  # 1 * 0.1 = 0.1 -> floors to 0 without the max(1, ...)

        self.assertTrue(limiter.allow("model-a", "acme"))


if __name__ == "__main__":
    unittest.main()
