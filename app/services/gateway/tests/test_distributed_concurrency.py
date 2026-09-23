"""Plan section 35.2 (P0 production hardening): DynamoDbConcurrencyLimiter
-- the distributed counterpart to concurrency.py's process-local
ConcurrencyLimiter. Uses moto's real DynamoDB emulation (not a
hand-rolled call-recording fake) specifically because this class's
whole point is getting DynamoDB's conditional-transaction semantics
right -- a fake that just records calls wouldn't catch a broken
ConditionExpression the way an engine that actually evaluates it will.
"""
import unittest

import boto3
from moto import mock_aws

from ..concurrency import DynamoDbConcurrencyLimiter

TABLE_NAME = "test-admission-control"
REGION = "us-east-1"


def _create_table(client) -> None:
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
class DynamoDbConcurrencyLimiterTests(unittest.TestCase):
    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        self.limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=10, default_tenant_max=3,
            client=self._client,
        )

    def test_acquire_succeeds_under_both_caps(self):
        self.assertTrue(self.limiter.try_acquire("acme"))
        self.assertEqual(self.limiter.current_global_count(), 1)

    def test_acquire_rejected_when_tenant_cap_exhausted(self):
        for _ in range(3):
            token = self.limiter.try_acquire("acme", tenant_max=3)
            self.assertTrue(token)

        self.assertFalse(self.limiter.try_acquire("acme", tenant_max=3))
        # Global counter must NOT have incremented on the rejected
        # attempt -- this is the whole point of using a transaction:
        # the tenant-cap failure must roll back the global increment too.
        self.assertEqual(self.limiter.current_global_count(), 3)

    def test_acquire_rejected_when_global_cap_exhausted_even_with_tenant_room(self):
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=2, default_tenant_max=100,
            client=self._client,
        )
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertTrue(limiter.try_acquire("other-tenant"))

        # acme has plenty of tenant-level room (max 100), but global is full.
        self.assertFalse(limiter.try_acquire("acme"))

    def test_two_tenants_are_isolated(self):
        for _ in range(3):
            token = self.limiter.try_acquire("acme", tenant_max=3)
            self.assertTrue(token)

        # acme is now exhausted, but a different tenant is unaffected.
        self.assertTrue(self.limiter.try_acquire("other-tenant", tenant_max=3))

    def test_release_frees_a_slot(self):
        token = self.limiter.try_acquire("acme")
        self.limiter.try_acquire("acme")
        self.assertEqual(self.limiter.current_global_count(), 2)

        self.limiter.release("acme", token)

        self.assertEqual(self.limiter.current_global_count(), 1)

    def test_release_after_acquiring_up_to_cap_allows_a_new_acquire(self):
        for _ in range(3):
            token = self.limiter.try_acquire("acme", tenant_max=3)
            self.assertTrue(token)
        self.assertFalse(self.limiter.try_acquire("acme", tenant_max=3))

        self.limiter.release("acme", token)

        self.assertTrue(self.limiter.try_acquire("acme", tenant_max=3))

    def test_double_release_does_not_go_negative_or_raise(self):
        token = self.limiter.try_acquire("acme")
        self.limiter.release("acme", token)

        self.limiter.release("acme", token)  # no raise

        self.assertEqual(self.limiter.current_global_count(), 0)

    def test_release_on_never_acquired_tenant_does_not_raise(self):
        self.limiter.release("never-acquired-tenant", "unknown")  # no raise

    def test_current_global_count_is_zero_before_any_acquire(self):
        self.assertEqual(self.limiter.current_global_count(), 0)

    def test_this_class_actually_coordinates_across_separate_instances(self):
        """The whole point of this class over the in-process one --
        two separate DynamoDbConcurrencyLimiter instances (standing in
        for two separate ECS tasks) share the same real counter."""
        limiter_task_a = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=2, default_tenant_max=10,
            client=self._client,
        )
        limiter_task_b = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=2, default_tenant_max=10,
            client=self._client,
        )

        self.assertTrue(limiter_task_a.try_acquire("acme"))
        self.assertTrue(limiter_task_b.try_acquire("acme"))
        # Global cap (2) is now exhausted across BOTH "tasks" combined --
        # a third acquire from either instance must be rejected, proving
        # they share real, coordinated state, not independent local counters.
        self.assertFalse(limiter_task_a.try_acquire("acme"))
        self.assertFalse(limiter_task_b.try_acquire("acme"))


@mock_aws
class DynamoDbConcurrencyLimiterLeaseTests(unittest.TestCase):
    """Plan section 35.18: the TTL'd per-request lease and its
    reconcile() sweep -- the actual fix for the crash-leak this
    class's own docstring used to flag as an unaddressed follow-up."""

    def setUp(self):
        self._client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self._client)
        # _RECONCILE_PROBABILITY effectively disabled (lease_ttl_s huge,
        # and no test here relies on try_acquire's own probabilistic
        # trigger) so every test controls reconcile() explicitly.
        self.limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=10, default_tenant_max=3,
            lease_ttl_s=300.0, client=self._client,
        )

    def test_try_acquire_returns_a_real_token_not_just_true(self):
        token = self.limiter.try_acquire("acme")
        self.assertIsInstance(token, str)
        self.assertTrue(token)

    def test_rejected_acquire_returns_none(self):
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=1, default_tenant_max=10,
            client=self._client,
        )
        limiter.try_acquire("acme")
        self.assertIsNone(limiter.try_acquire("other"))

    def test_two_concurrent_leases_for_the_same_tenant_are_independent(self):
        """Distinct uuid4 lease ids -- releasing one must not free the
        other's slot."""
        token_1 = self.limiter.try_acquire("acme")
        token_2 = self.limiter.try_acquire("acme")
        self.assertNotEqual(token_1, token_2)
        self.assertEqual(self.limiter.current_global_count(), 2)

        self.limiter.release("acme", token_1)

        self.assertEqual(self.limiter.current_global_count(), 1)

    def test_release_with_lease_token_deletes_the_lease_item(self):
        token = self.limiter.try_acquire("acme")
        self.limiter.release("acme", token)

        item = self._client.get_item(
            TableName=TABLE_NAME, Key={"pk": {"S": f"lease#acme#{token}"}}
        ).get("Item")
        self.assertIsNone(item)

    def test_release_without_token_is_rejected_without_decrement(self):
        self.limiter.try_acquire("acme")
        with self.assertRaises(ValueError):
            self.limiter.release("acme")
        self.assertEqual(self.limiter.current_global_count(), 1)

    def test_reconcile_sweeps_an_expired_lease_and_frees_its_slot(self):
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=1, default_tenant_max=1,
            lease_ttl_s=1.0, client=self._client,
        )
        limiter.try_acquire("acme")  # process "crashes" here, never releases
        self.assertEqual(limiter.current_global_count(), 1)
        self.assertFalse(limiter.try_acquire("other"))  # global cap still held

        swept = limiter.reconcile(now=__import__("time").time() + 100)  # well past lease_ttl_s

        self.assertEqual(swept, 1)
        self.assertEqual(limiter.current_global_count(), 0)

    def test_reconcile_does_not_touch_a_lease_still_within_its_ttl(self):
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=1, default_tenant_max=1,
            lease_ttl_s=300.0, client=self._client,
        )
        limiter.try_acquire("acme")

        swept = limiter.reconcile()  # "now" -- lease has 300s left

        self.assertEqual(swept, 0)
        self.assertEqual(limiter.current_global_count(), 1)  # still held, correctly

    def test_reconcile_deletes_the_lease_item_it_compensates(self):
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=1, default_tenant_max=1,
            lease_ttl_s=1.0, client=self._client,
        )
        token = limiter.try_acquire("acme")

        limiter.reconcile(now=__import__("time").time() + 100)

        item = self._client.get_item(
            TableName=TABLE_NAME, Key={"pk": {"S": f"lease#acme#{token}"}}
        ).get("Item")
        self.assertIsNone(item)

    def test_reconcile_racing_a_clean_release_does_not_double_compensate(self):
        """The exact race this design has to be safe against: a
        request finishes normally (release()) at roughly the same time
        reconcile() decides that same lease looks stale. Whichever
        transaction commits first wins; the other's conditional Delete
        cancels harmlessly -- the counter must end up decremented
        exactly once, not twice (which would let it go negative-
        equivalent -- an over-admission bug worse than the leak this
        whole feature fixes)."""
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=5, default_tenant_max=5,
            lease_ttl_s=1.0, client=self._client,
        )
        token = limiter.try_acquire("acme")
        self.assertEqual(limiter.current_global_count(), 1)

        # Simulate: release() runs first (the request actually finished
        # cleanly), THEN a reconcile() pass (from another instance) scans
        # the now-expired-looking lease -- but it's already gone.
        limiter.release("acme", token)
        swept = limiter.reconcile(now=__import__("time").time() + 100)

        self.assertEqual(swept, 0)  # nothing left to sweep
        self.assertEqual(limiter.current_global_count(), 0)  # exactly one decrement total

    def test_reconcile_with_multiple_expired_leases_across_tenants(self):
        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=10, default_tenant_max=10,
            lease_ttl_s=1.0, client=self._client,
        )
        limiter.try_acquire("acme")
        limiter.try_acquire("acme")
        limiter.try_acquire("finance")
        self.assertEqual(limiter.current_global_count(), 3)

        swept = limiter.reconcile(now=__import__("time").time() + 100)

        self.assertEqual(swept, 3)
        self.assertEqual(limiter.current_global_count(), 0)

    def test_try_acquire_probabilistically_triggers_reconcile(self):
        """_RECONCILE_PROBABILITY=1.0 forces the trigger on every call
        -- proves try_acquire actually wires up self-healing, not just
        that reconcile() works when called directly."""
        from unittest.mock import patch

        limiter = DynamoDbConcurrencyLimiter(
            table_name=TABLE_NAME, region=REGION, global_max=10, default_tenant_max=10,
            lease_ttl_s=1.0, client=self._client,
        )
        limiter.try_acquire("acme")  # one abandoned lease, about to expire
        self.assertEqual(limiter.current_global_count(), 1)

        import time as _time

        with patch.object(type(limiter), "_RECONCILE_PROBABILITY", 1.0), \
             patch.object(_time, "time", return_value=_time.time() + 100):
            limiter.try_acquire("other")  # triggers reconcile() before its own acquire

        # The abandoned "acme" lease was swept (freeing its slot) before
        # "other" was admitted -- both increments plus the sweep net to 1.
        self.assertEqual(limiter.current_global_count(), 1)


if __name__ == "__main__":
    unittest.main()
