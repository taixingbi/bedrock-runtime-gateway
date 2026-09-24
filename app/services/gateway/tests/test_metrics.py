"""telemetry/metrics.py -- CloudWatch EMF emission. Captures raw
stdout (this module deliberately bypasses telemetry/logging.py's
JsonFormatter -- see the module's own docstring for why) and parses it
back as JSON, since EMF's whole contract IS "a specific JSON shape on
stdout" -- there's no separate API to call.
"""
import contextlib
import io
import json
import unittest

from ..telemetry.metrics import emit_request_metric


class EmitRequestMetricTests(unittest.TestCase):
    def _emit_and_capture(self, **kwargs) -> dict:
        kwargs.setdefault("environment", "dev")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            emit_request_metric(**kwargs)
        lines = [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_request_count_always_present(self):
        line = self._emit_and_capture(tenant_id="acme")

        self.assertEqual(line["RequestCount"], 1)
        self.assertEqual(line["tenant_id"], "acme")

    def test_environment_is_always_a_real_dimension(self):
        """environment has no default and rides in EVERY dimension set,
        including the "global" one -- a shared CloudWatch namespace
        across dev/prod must never let their metrics blend (see the
        module's own docstring)."""
        line = self._emit_and_capture(tenant_id="acme", environment="prod")

        self.assertEqual(line["environment"], "prod")
        dims = line["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
        self.assertIn(["environment", "tenant_id"], dims)
        self.assertIn(["environment"], dims)

    def test_dimension_sets_are_tenant_and_global(self):
        """The actual "global AND per-tenant from one emission" claim
        -- the environment-only dimension set means CloudWatch also
        rolls this metric up per-environment (the "global" view),
        alongside the per-tenant one."""
        line = self._emit_and_capture(tenant_id="acme")

        dims = line["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
        self.assertIn(["environment", "tenant_id"], dims)
        self.assertIn(["environment"], dims)

    def test_model_dimension_added_only_when_model_is_known(self):
        line = self._emit_and_capture(tenant_id="acme", model="model-a")

        dims = line["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
        self.assertIn(["environment", "tenant_id", "model"], dims)
        self.assertEqual(line["model"], "model-a")

    def test_no_model_means_no_model_dimension_or_field(self):
        line = self._emit_and_capture(tenant_id="acme")

        dims = line["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
        self.assertNotIn(["environment", "tenant_id", "model"], dims)
        self.assertNotIn("model", line)

    def test_e2e_latency_included_when_provided(self):
        line = self._emit_and_capture(tenant_id="acme", e2e_latency_ms=123.4)

        self.assertEqual(line["E2ELatencyMs"], 123.4)
        metric_names = {m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
        self.assertIn("E2ELatencyMs", metric_names)

    def test_e2e_latency_omitted_not_zeroed_when_not_provided(self):
        line = self._emit_and_capture(tenant_id="acme")

        self.assertNotIn("E2ELatencyMs", line)

    def test_ttft_included_only_for_streaming_when_provided(self):
        line = self._emit_and_capture(tenant_id="acme", ttft_ms=42.0)

        self.assertEqual(line["TTFTMs"], 42.0)

    def test_ttft_omitted_for_non_streaming(self):
        line = self._emit_and_capture(tenant_id="acme", e2e_latency_ms=100.0)  # no ttft_ms

        self.assertNotIn("TTFTMs", line)

    def test_cost_included_only_on_success(self):
        line = self._emit_and_capture(tenant_id="acme", estimated_cost_usd=0.0012)

        self.assertEqual(line["EstimatedCostUsd"], 0.0012)

    def test_error_flag_adds_error_count(self):
        line = self._emit_and_capture(tenant_id="acme", error=True)

        self.assertEqual(line["ErrorCount"], 1)

    def test_no_error_means_no_error_count(self):
        line = self._emit_and_capture(tenant_id="acme")

        self.assertNotIn("ErrorCount", line)

    def test_reject_stage_adds_reject_count_and_stage_attribute(self):
        line = self._emit_and_capture(tenant_id="acme", reject_stage="rate_limit")

        self.assertEqual(line["RejectCount"], 1)
        self.assertEqual(line["reject_stage"], "rate_limit")

    def test_no_reject_stage_means_no_reject_count(self):
        line = self._emit_and_capture(tenant_id="acme")

        self.assertNotIn("RejectCount", line)
        self.assertNotIn("reject_stage", line)

    def test_namespace_is_bedrock_gateway(self):
        line = self._emit_and_capture(tenant_id="acme")

        self.assertEqual(line["_aws"]["CloudWatchMetrics"][0]["Namespace"], "BedrockGateway")

    def test_a_full_success_emission_carries_every_relevant_field(self):
        line = self._emit_and_capture(
            tenant_id="acme", model="model-a", e2e_latency_ms=250.0, ttft_ms=80.0,
            estimated_cost_usd=0.002,
        )

        self.assertEqual(line["tenant_id"], "acme")
        self.assertEqual(line["model"], "model-a")
        self.assertEqual(line["E2ELatencyMs"], 250.0)
        self.assertEqual(line["TTFTMs"], 80.0)
        self.assertEqual(line["EstimatedCostUsd"], 0.002)
        self.assertNotIn("ErrorCount", line)
        self.assertNotIn("RejectCount", line)


if __name__ == "__main__":
    unittest.main()
