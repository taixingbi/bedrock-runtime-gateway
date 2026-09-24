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


if __name__ == "__main__":
    unittest.main()
