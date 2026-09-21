"""Plan section 35.13 (P1 production hardening):
scripts/migrate_file_tenants_to_dynamodb.py -- the concrete tool for
finishing the file->DynamoDB policy migration LayeredPolicyStore's own
design left open-ended. Real DynamoDB emulation via moto, same
reasoning as test_distributed_concurrency.py.
"""
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws

TABLE_NAME = "test-provisioned-tenant-policies"
REGION = "us-east-1"

migrate = import_module("scripts.migrate_file_tenants_to_dynamodb")


def _create_table(client) -> None:
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _write_tenants_yaml(tmp_dir: str, tenants: dict) -> str:
    path = Path(tmp_dir) / "tenants.yaml"
    body = "tenants:\n"
    for tenant_id, cfg in tenants.items():
        body += f"  {tenant_id}:\n"
        for k, v in cfg.items():
            body += f"    {k}: {v}\n"
    path.write_text(body)
    return str(path)


@mock_aws
class MigrateFileTenantsScriptTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("dynamodb", region_name=REGION)
        _create_table(self.client)

    def _run(self, argv):
        with patch.object(sys, "argv", ["migrate_file_tenants_to_dynamodb.py"] + argv):
            return migrate.main()

    def test_dry_run_does_not_write_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_tenants_yaml(tmp, {"acme": {"rpm_limit": 60, "state": "ACTIVE"}})

            exit_code = self._run(["--tenants-path", path, "--table-name", TABLE_NAME, "--region", REGION])

            self.assertEqual(exit_code, 0)
            response = self.client.scan(TableName=TABLE_NAME)
            self.assertEqual(response["Items"], [])

    def test_apply_migrates_tenants_into_dynamodb(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_tenants_yaml(
                tmp, {"acme": {"rpm_limit": 60, "state": "ACTIVE"}, "other": {"rpm_limit": 30, "state": "ACTIVE"}}
            )

            exit_code = self._run(
                ["--tenants-path", path, "--table-name", TABLE_NAME, "--region", REGION, "--apply"]
            )

            self.assertEqual(exit_code, 0)
            response = self.client.scan(TableName=TABLE_NAME)
            migrated_ids = {item["tenant_id"]["S"] for item in response["Items"]}
            self.assertEqual(migrated_ids, {"acme", "other"})

    def test_apply_is_idempotent_already_migrated_tenant_is_skipped_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_tenants_yaml(tmp, {"acme": {"rpm_limit": 60, "state": "ACTIVE"}})

            self._run(["--tenants-path", path, "--table-name", TABLE_NAME, "--region", REGION, "--apply"])
            # Manually drift the DynamoDB copy to prove a second run
            # doesn't clobber it back to the file's version.
            self.client.update_item(
                TableName=TABLE_NAME,
                Key={"tenant_id": {"S": "acme"}},
                UpdateExpression="SET rpm_limit = :v",
                ExpressionAttributeValues={":v": {"N": "9999"}},
            )

            exit_code = self._run(
                ["--tenants-path", path, "--table-name", TABLE_NAME, "--region", REGION, "--apply"]
            )

            self.assertEqual(exit_code, 0)
            response = self.client.get_item(TableName=TABLE_NAME, Key={"tenant_id": {"S": "acme"}})
            self.assertEqual(response["Item"]["rpm_limit"]["N"], "9999")  # untouched by the second run

    def test_empty_tenants_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_tenants_yaml(tmp, {})

            exit_code = self._run(["--tenants-path", path, "--table-name", TABLE_NAME, "--region", REGION, "--apply"])

            self.assertEqual(exit_code, 0)
            response = self.client.scan(TableName=TABLE_NAME)
            self.assertEqual(response["Items"], [])


if __name__ == "__main__":
    unittest.main()
