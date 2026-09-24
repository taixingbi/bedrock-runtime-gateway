"""Plan section 35.2 (P0 production hardening): DynamoDbRateLimiter --
the distributed counterpart to policy/rate_limiter.py's process-local
TokenBucketRateLimiter. Real DynamoDB emulation via moto (not a
call-recording fake), same reasoning as
test_distributed_concurrency.py -- this class's whole point is
getting DynamoDB's conditional-write semantics right.
"""
import unittest

import boto3
from moto import mock_aws

from ..policy.rate_limiter import DynamoDbRateLimiter

TABLE_NAME = "test-admission-control"
REGION = "us-east-1"


def _create_table(client) -> None:
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@mock_aws
class DynamoDbRateLimiterTests(unittest.TestCase):
    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.clock = FakeClock()
        self.limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )

    def test_first_request_for_a_tenant_is_allowed(self):
        self.assertTrue(self.limiter.allow("acme", rpm_limit=60))

    def test_exceeding_budget_is_rejected(self):
        limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        self.assertTrue(limiter.allow("acme", rpm_limit=1))

        self.assertFalse(limiter.allow("acme", rpm_limit=1))

    def test_tenants_are_isolated(self):
        self.assertTrue(self.limiter.allow("tenant-a", rpm_limit=1))
        self.assertFalse(self.limiter.allow("tenant-a", rpm_limit=1))

        self.assertTrue(self.limiter.allow("tenant-b", rpm_limit=1))  # unaffected by tenant-a

    def test_refills_over_time(self):
        limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        self.assertTrue(limiter.allow("acme", rpm_limit=60))  # capacity=60, consume 1 -> 59 left
        for _ in range(59):
            self.assertTrue(limiter.allow("acme", rpm_limit=60))
        self.assertFalse(limiter.allow("acme", rpm_limit=60))  # exhausted

        self.clock.advance(1.0)  # 1s at 1 token/s refill rate (60/60)

        self.assertTrue(limiter.allow("acme", rpm_limit=60))

    def test_zero_rpm_limit_never_allows(self):
        self.assertFalse(self.limiter.allow("acme", rpm_limit=0))

    def test_two_instances_share_the_same_bucket(self):
        """The whole point of this class over the in-process one --
        two separate DynamoDbRateLimiter instances (standing in for
        two separate ECS tasks) share the same real bucket."""
        limiter_task_a = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        limiter_task_b = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )

        self.assertTrue(limiter_task_a.allow("acme", rpm_limit=2))
        self.assertTrue(limiter_task_b.allow("acme", rpm_limit=2))
        # Combined, both tasks have now consumed the tenant's entire
        # budget (2) -- a third call from EITHER task must be rejected,
        # proving they share real, coordinated state.
        self.assertFalse(limiter_task_a.allow("acme", rpm_limit=2))
        self.assertFalse(limiter_task_b.allow("acme", rpm_limit=2))


@mock_aws
class DynamoDbRateLimiterAmountTests(unittest.TestCase):
    """`amount` (default 1.0, every RPM caller's exact prior behavior)
    -- pipeline.enforce_token_rate_limit is the real caller that passes
    a variable amount (an estimated token count); these test the
    underlying mechanic directly, independent of TPM."""

    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.clock = FakeClock()

    def test_default_amount_is_one_unchanged_from_before_this_param_existed(self):
        limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        self.assertTrue(limiter.allow("acme", rpm_limit=1))
        self.assertFalse(limiter.allow("acme", rpm_limit=1))

    def test_a_single_call_can_consume_more_than_one_unit(self):
        limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        self.assertTrue(limiter.allow("acme", rpm_limit=1000, amount=999.0))
        self.assertFalse(limiter.allow("acme", rpm_limit=1000, amount=2.0))  # only 1 left
        self.assertTrue(limiter.allow("acme", rpm_limit=1000, amount=1.0))  # exactly what's left

    def test_amount_exceeding_full_capacity_is_always_rejected(self):
        limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        self.assertFalse(limiter.allow("acme", rpm_limit=100, amount=101.0))

    def test_rejected_amount_consumes_nothing(self):
        limiter = DynamoDbRateLimiter(
            table_name=TABLE_NAME, region=REGION, clock=self.clock, client=self._client,
        )
        self.assertFalse(limiter.allow("acme", rpm_limit=10, amount=11.0))
        self.assertTrue(limiter.allow("acme", rpm_limit=10, amount=10.0))  # full budget still intact


if __name__ == "__main__":
    unittest.main()
