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


def _put_quota(client, model_id: str, rpm_limit: int, tpm_limit=None) -> None:
    item = {
        "pk": {"S": f"quota#{model_id}"},
        "rpm_limit": {"N": str(rpm_limit)},
        "quota_type": {"S": "cross_region"},
    }
    if tpm_limit is not None:
        item["tpm_limit"] = {"N": str(tpm_limit)}
    client.put_item(TableName=TABLE_NAME, Item=item)


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

    def test_unknown_model_tpm_limit_is_none(self):
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock)
        self.assertIsNone(cache.tpm_limit_for("no-such-model"))

    def test_synced_model_returns_its_tpm_limit(self):
        _put_quota(self._client, "model-a", 50, tpm_limit=8_000_000)
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock)

        self.assertEqual(cache.tpm_limit_for("model-a"), 8_000_000)

    def test_rpm_only_sync_leaves_tpm_limit_none(self):
        """A model synced before TPM support existed, or one AWS has
        no discoverable TPM quota for -- see sync script's own 'RPM
        required, TPM best-effort' docstring."""
        _put_quota(self._client, "model-a", 50)  # no tpm_limit
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock)

        self.assertEqual(cache.rpm_limit_for("model-a"), 50)
        self.assertIsNone(cache.tpm_limit_for("model-a"))

    def test_tpm_limit_reads_are_cached_together_with_rpm(self):
        """rpm_limit_for and tpm_limit_for share the same cached row --
        a re-sync mid-TTL must not be observed by either accessor,
        proving there's no separate, independently-refreshing cache
        entry for tpm_limit_for that could drift from rpm_limit_for's."""
        _put_quota(self._client, "model-a", 50, tpm_limit=1_000_000)
        cache = ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock, ttl_s=60.0)
        self.assertEqual(cache.rpm_limit_for("model-a"), 50)
        self.assertEqual(cache.tpm_limit_for("model-a"), 1_000_000)

        _put_quota(self._client, "model-a", 999, tpm_limit=9_999_999)
        self.clock.advance(30.0)  # still within TTL

        self.assertEqual(cache.rpm_limit_for("model-a"), 50)
        self.assertEqual(cache.tpm_limit_for("model-a"), 1_000_000)


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


@mock_aws
class ModelQuotaLimiterTpmTests(unittest.TestCase):
    """TPM enforcement -- additive to the RPM gate covered above, see
    model_quota.py's own docstring on why both are checked
    independently rather than TPM replacing RPM."""

    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.clock = FakeClock()

    def _limiter(self, *, with_tenant_tpm: bool = False, per_tenant_share_pct: float = 0.4) -> ModelQuotaLimiter:
        return ModelQuotaLimiter(
            cache=ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock),
            limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model#",
            ),
            tpm_limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model_tpm#",
            ),
            tenant_tpm_limiter=(
                DynamoDbRateLimiter(
                    table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                    key_prefix="ratelimit#model_tenant_tpm#",
                )
                if with_tenant_tpm else None
            ),
            per_tenant_share_pct=per_tenant_share_pct,
        )

    def test_no_estimated_tokens_skips_the_tpm_gate_entirely(self):
        """Backward compatible: a caller that never passes
        estimated_tokens (e.g. an older call site) is gated on RPM
        only, exactly as if no tpm_limiter were configured at all."""
        _put_quota(self._client, "model-a", 10, tpm_limit=5)  # tiny tpm budget
        limiter = self._limiter()

        for _ in range(10):
            self.assertTrue(limiter.allow("model-a"))  # no estimated_tokens -- tpm never checked

    def test_no_tpm_limiter_configured_skips_the_tpm_gate(self):
        _put_quota(self._client, "model-a", 10, tpm_limit=5)
        limiter = ModelQuotaLimiter(
            cache=ModelQuotaCache(table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock),
            limiter=DynamoDbRateLimiter(
                table_name=TABLE_NAME, region=REGION, client=self._client, clock=self.clock,
                key_prefix="ratelimit#model#",
            ),
        )

        self.assertTrue(limiter.allow("model-a", estimated_tokens=1000))  # would blow a tpm_limit=5 budget

    def test_unsynced_tpm_limit_skips_the_tpm_gate_even_with_estimated_tokens(self):
        """RPM-only sync (see sync script's own docstring) -- no
        tpm_limit on the row at all means unknown, not zero."""
        _put_quota(self._client, "model-a", 10)  # no tpm_limit
        limiter = self._limiter()

        for _ in range(10):
            self.assertTrue(limiter.allow("model-a", estimated_tokens=1_000_000))

    def test_synced_tpm_limit_is_enforced(self):
        _put_quota(self._client, "model-a", 1000, tpm_limit=100)  # generous rpm, tiny tpm
        limiter = self._limiter()

        self.assertTrue(limiter.allow("model-a", estimated_tokens=60))
        self.assertFalse(limiter.allow("model-a", estimated_tokens=60))  # 120 > 100 budget

    def test_tpm_and_rpm_budgets_are_independent(self):
        _put_quota(self._client, "model-a", 1, tpm_limit=1_000_000)  # rpm=1, generous tpm
        limiter = self._limiter()

        self.assertTrue(limiter.allow("model-a", estimated_tokens=10))
        # RPM budget (1) is now exhausted even though TPM has plenty left.
        self.assertFalse(limiter.allow("model-a", estimated_tokens=10))

    def test_per_tenant_tpm_fair_share_caps_a_single_tenant(self):
        _put_quota(self._client, "model-a", 1000, tpm_limit=100)  # per-tenant tpm share: 100*0.4=40
        limiter = self._limiter(with_tenant_tpm=True, per_tenant_share_pct=0.4)

        self.assertTrue(limiter.allow("model-a", "busy-tenant", estimated_tokens=40))
        # This tenant's own 40-token share is exhausted, even though
        # the model-wide 100-token tpm budget still has 60 left.
        self.assertFalse(limiter.allow("model-a", "busy-tenant", estimated_tokens=1))

    def test_a_different_tenant_is_unaffected_by_one_tenants_exhausted_tpm_share(self):
        _put_quota(self._client, "model-a", 1000, tpm_limit=100)
        limiter = self._limiter(with_tenant_tpm=True, per_tenant_share_pct=0.4)

        self.assertTrue(limiter.allow("model-a", "busy-tenant", estimated_tokens=40))
        self.assertFalse(limiter.allow("model-a", "busy-tenant", estimated_tokens=1))

        self.assertTrue(limiter.allow("model-a", "quiet-tenant", estimated_tokens=40))


if __name__ == "__main__":
    unittest.main()
