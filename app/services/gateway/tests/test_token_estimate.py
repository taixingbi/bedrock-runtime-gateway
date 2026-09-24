import unittest
from dataclasses import dataclass

from ..usage.token_estimate import estimate_tokens


@dataclass
class _Msg:
    content: str


class EstimateTokensTests(unittest.TestCase):
    def test_reserves_max_tokens_as_worst_case_output(self):
        # 0-length input -> estimated_input_tokens floors at 1 (max(1, ...)).
        self.assertEqual(estimate_tokens([_Msg(content="")], max_tokens=500), 1 + 500)

    def test_estimates_input_tokens_at_roughly_4_chars_per_token(self):
        # 40 chars // 4 == 10 estimated input tokens.
        self.assertEqual(estimate_tokens([_Msg(content="x" * 40)], max_tokens=100), 10 + 100)

    def test_sums_across_multiple_messages(self):
        messages = [_Msg(content="x" * 40), _Msg(content="x" * 40)]
        self.assertEqual(estimate_tokens(messages, max_tokens=0), 20)

    def test_never_returns_less_than_one_input_token_even_for_a_single_char(self):
        self.assertEqual(estimate_tokens([_Msg(content="x")], max_tokens=0), 1)


if __name__ == "__main__":
    unittest.main()
