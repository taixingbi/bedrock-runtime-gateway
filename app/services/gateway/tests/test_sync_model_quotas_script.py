"""scripts/sync_model_quotas_from_aws.py -- pulls each certified
model's real AWS Service Quotas RPM value into gateway-model-quotas-
dev's "quota#<model_id>" row. Real DynamoDB emulation via moto for the
write half (same reasoning as every other migration script here);
service-quotas has no moto support, so that half uses a hand-written
fake client instead -- see the script's own docstring.
"""
import unittest
from importlib import import_module

import boto3
from moto import mock_aws

TABLE_NAME = "test-model-quotas"
REGION = "us-east-1"

sync_script = import_module("scripts.sync_model_quotas_from_aws")


def _create_table(client) -> None:
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


class _FakePaginator:
    def __init__(self, quotas):
        self._quotas = quotas

    def paginate(self, **_kwargs):
        # A single page is enough to exercise the pagination call shape
        # without needing a real multi-page AWS response.
        yield {"Quotas": self._quotas}


class FakeServiceQuotasClient:
    def __init__(self, quotas):
        self._quotas = quotas

    def get_paginator(self, operation_name):
        assert operation_name == "list_service_quotas"
        return _FakePaginator(self._quotas)


def _quota(name: str, value: float) -> dict:
    return {"QuotaName": name, "Value": value}


_FULL_QUOTA_SET = [
    _quota("Cross-region model inference requests per minute for Amazon Nova Micro", 400),
    _quota("Cross-region model inference requests per minute for Amazon Nova Lite", 400),
    _quota("Cross-region model inference requests per minute for Amazon Nova Pro", 50),
    _quota("Cross-region model inference requests per minute for Meta Llama 3.3 70B Instruct", 80),
    _quota("On-demand model inference requests per minute for Qwen3 32B V1", 1000),
]


@mock_aws
class SyncModelQuotasScriptTests(unittest.TestCase):
    def setUp(self):
        self.dynamo_client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self.dynamo_client)

    def test_dry_run_does_not_write_anything(self):
        sq_client = FakeServiceQuotasClient(_FULL_QUOTA_SET)

        synced, skipped = sync_script.sync_quotas(
            sq_client=sq_client, dynamo_client=None, table_name=TABLE_NAME, apply=False,
        )

        self.assertEqual(synced, [])
        self.assertEqual(skipped, [])
        response = self.dynamo_client.scan(TableName=TABLE_NAME)
        self.assertEqual(response["Items"], [])

    def test_apply_writes_every_mapped_models_rpm_limit(self):
        sq_client = FakeServiceQuotasClient(_FULL_QUOTA_SET)

        synced, skipped = sync_script.sync_quotas(
            sq_client=sq_client, dynamo_client=self.dynamo_client, table_name=TABLE_NAME, apply=True,
        )

        self.assertEqual(set(synced), set(sync_script.MODEL_QUOTA_NAMES.keys()))
        self.assertEqual(skipped, [])

        item = self.dynamo_client.get_item(
            TableName=TABLE_NAME, Key={"pk": {"S": "quota#us.amazon.nova-pro-v1:0"}}
        )["Item"]
        self.assertEqual(item["rpm_limit"]["N"], "50")
        self.assertEqual(item["quota_type"]["S"], "cross_region")

        item = self.dynamo_client.get_item(
            TableName=TABLE_NAME, Key={"pk": {"S": "quota#qwen.qwen3-32b-v1:0"}}
        )["Item"]
        self.assertEqual(item["rpm_limit"]["N"], "1000")
        self.assertEqual(item["quota_type"]["S"], "on_demand")

    def test_missing_quota_in_account_is_skipped_not_a_hard_failure(self):
        # Only 4 of the 5 mapped quotas are "present in this account".
        partial = [q for q in _FULL_QUOTA_SET if "Nova Pro" not in q["QuotaName"]]
        sq_client = FakeServiceQuotasClient(partial)

        synced, skipped = sync_script.sync_quotas(
            sq_client=sq_client, dynamo_client=self.dynamo_client, table_name=TABLE_NAME, apply=True,
        )

        self.assertNotIn("us.amazon.nova-pro-v1:0", synced)
        self.assertEqual([model_id for model_id, _ in skipped], ["us.amazon.nova-pro-v1:0"])
        response = self.dynamo_client.get_item(
            TableName=TABLE_NAME, Key={"pk": {"S": "quota#us.amazon.nova-pro-v1:0"}}
        )
        self.assertNotIn("Item", response)

    def test_apply_is_idempotent_and_never_touches_the_live_counter_row(self):
        """Re-running after real traffic has already written a
        ratelimit#model# counter row for the same model_id must never
        touch that row -- only the separate quota# config row."""
        self.dynamo_client.put_item(
            TableName=TABLE_NAME,
            Item={
                "pk": {"S": "ratelimit#model#us.amazon.nova-pro-v1:0"},
                "tokens": {"N": "12.5"},
                "last_refill_ms": {"N": "1700000000000"},
            },
        )
        sq_client = FakeServiceQuotasClient(_FULL_QUOTA_SET)

        sync_script.sync_quotas(
            sq_client=sq_client, dynamo_client=self.dynamo_client, table_name=TABLE_NAME, apply=True,
        )
        sync_script.sync_quotas(
            sq_client=sq_client, dynamo_client=self.dynamo_client, table_name=TABLE_NAME, apply=True,
        )

        counter_row = self.dynamo_client.get_item(
            TableName=TABLE_NAME, Key={"pk": {"S": "ratelimit#model#us.amazon.nova-pro-v1:0"}}
        )["Item"]
        self.assertEqual(counter_row["tokens"]["N"], "12.5")  # untouched by either sync run


if __name__ == "__main__":
    unittest.main()
