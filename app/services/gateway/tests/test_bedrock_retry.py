"""BedrockClient's own retry loop: exponential backoff with full jitter,
an optional cap per sleep and an optional total retry budget."""
import unittest
from unittest import mock

from services.gateway.inference.bedrock_client import BedrockChatMessage, BedrockClient, BedrockInvocationError


class _Throttled(Exception):
    def __init__(self):
        super().__init__("throttled")
        self.response = {"Error": {"Code": "ThrottlingException"}}


class _FailingThen:
    """Raises `failures` throttles, then succeeds."""
    def __init__(self, failures: int):
        self.failures, self.calls = failures, 0

    def converse(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise _Throttled()
        return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {"inputTokens": 1, "outputTokens": 1}}


def _client(fake, **kwargs) -> BedrockClient:
    with mock.patch("boto3.client"):
        client = BedrockClient(region="us-east-1", **kwargs)
    client._client = fake
    return client


MSG = [BedrockChatMessage(role="user", text="hi")]


class BedrockRetryTests(unittest.TestCase):
    def test_default_behaviour_is_unchanged(self):
        """No cap, no budget: sleeps uniform(0, 0.25 x 2^(k-1))."""
        fake = _FailingThen(2)
        with mock.patch("time.sleep") as sleep, mock.patch("random.uniform", side_effect=lambda a, b: b):
            result = _client(fake).converse(model_id="m", messages=MSG)
        self.assertEqual((result.retry_count, fake.calls), (2, 3))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])

    def test_max_backoff_caps_each_sleep(self):
        fake = _FailingThen(4)
        with mock.patch("time.sleep") as sleep, mock.patch("random.uniform", side_effect=lambda a, b: b):
            _client(fake, max_retries=4, base_backoff_s=0.1, max_backoff_s=0.3).converse(model_id="m", messages=MSG)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.1, 0.2, 0.3, 0.3])

    def test_retry_budget_stops_retrying(self):
        fake = _FailingThen(5)
        client = _client(fake, max_retries=5, base_backoff_s=1.0, total_retry_budget_s=1.5)
        with mock.patch("time.sleep") as sleep, mock.patch("random.uniform", side_effect=lambda a, b: b):
            with self.assertRaises(BedrockInvocationError) as ctx:
                client.converse(model_id="m", messages=MSG)
        # 1st retry sleeps 1.0 s (within 1.5); the 2nd would add 2.0 s -> budget exhausted.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0])
        self.assertEqual(fake.calls, 2)
        self.assertIn("retry budget", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)

    def test_invalid_knobs_are_rejected(self):
        for kwargs in ({"max_backoff_s": 0}, {"total_retry_budget_s": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                _client(_FailingThen(0), **kwargs)


if __name__ == "__main__":
    unittest.main()
