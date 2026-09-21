"""M6 scenarios: kill-switch propagation and policy updates while traffic
is in flight (plan section 17's "kill-switch propagation" and "policy
update under load" scenarios, plan section 8's bounded-propagation
guarantee).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loadtests.fault_injection import AlwaysAllowGuardrailClient, ThrottlingFaultConverseClient
from loadtests.harness import build_scenario_app, default_tenant_policy, fire_concurrent
from services.gateway.policy.models import TenantState


class KillSwitchUnderLoadScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_kill_switch_blocks_traffic_immediately_after_flip(self):
        fake = ThrottlingFaultConverseClient(throttle_rate=0.0)
        scenario = build_scenario_app(
            tenants={"acme": default_tenant_policy("acme", rpm_limit=10_000)},
            converse_client=fake,
            guardrail_client=AlwaysAllowGuardrailClient(),
        )

        async def make_request(i: int):
            return await scenario.client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": f"msg-{i}"}]},
                headers=scenario.auth_header(tenant_id="acme"),
            )

        try:
            pre = await fire_concurrent(make_request, 10)
            self.assertTrue(all(r.status_code == 200 for r in pre))
            calls_before_block = fake.total_calls

            # The admin API that used to flip this over HTTP (M2's
            # push-invalidation) is now platform-control-plane's own
            # backend, not this repo's -- call policy_store/policy_cache
            # directly, the same two calls that endpoint used to make.
            scenario.policy_store.set_state("acme", TenantState.EMERGENCY_BLOCK)
            scenario.policy_cache.invalidate("acme")

            # No wait for any TTL -- push invalidation (M2) means the
            # very next requests already see the new state.
            post = await fire_concurrent(make_request, 10)
        finally:
            await scenario.aclose()

        self.assertTrue(all(r.status_code == 403 for r in post))
        self.assertTrue(all(r.json()["error"]["code"] == "TENANT_BLOCKED" for r in post))
        # None of the post-block requests reached the model.
        self.assertEqual(fake.total_calls, calls_before_block)


class PolicyUpdateUnderLoadScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_epoch_bump_invalidates_cache_under_concurrent_access(self):
        fake = ThrottlingFaultConverseClient(throttle_rate=0.0, response_text="answer-v1")
        scenario = build_scenario_app(
            tenants={"acme": default_tenant_policy("acme", rpm_limit=10_000)},
            converse_client=fake,
            guardrail_client=AlwaysAllowGuardrailClient(),
        )
        body = {"messages": [{"role": "user", "content": "same question every time"}]}

        async def make_request(_i: int):
            return await scenario.client.post(
                "/v1/chat", json=body, headers=scenario.auth_header(tenant_id="acme")
            )

        try:
            warm = await scenario.client.post(
                "/v1/chat", json=body, headers=scenario.auth_header(tenant_id="acme")
            )
            self.assertEqual(warm.status_code, 200)
            calls_after_warm = fake.total_calls

            # A concurrent burst of identical requests should all be
            # cache hits -- no new calls to the model at all.
            burst1 = await fire_concurrent(make_request, 20)
            self.assertTrue(all(r.json()["cache_hit"] for r in burst1))
            self.assertEqual(fake.total_calls, calls_after_warm)

            # Bump policy_epoch directly (same two calls the admin
            # endpoint used to make over HTTP -- see the kill-switch
            # scenario above) while "in flight", immediately before the
            # next burst.
            scenario.policy_store.set_state("acme", TenantState.ACTIVE)  # unchanged state, still bumps policy_epoch
            scenario.policy_cache.invalidate("acme")

            burst2 = await fire_concurrent(make_request, 20)
        finally:
            await scenario.aclose()

        # At least one request after the bump had to miss and recompute
        # -- the pre-bump cache entries cannot leak past the policy
        # change, even with many requests racing to read/write the cache
        # at once.
        self.assertTrue(any(not r.json()["cache_hit"] for r in burst2))


if __name__ == "__main__":
    unittest.main()
