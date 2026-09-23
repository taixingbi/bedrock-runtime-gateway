#!/usr/bin/env python3
"""Backfills hand-managed (tenants.yaml) tenants into the DynamoDB
provisioned-tenant-policies table (plan section 35.13, P1 production
hardening).

The real gap this closes: `LayeredPolicyStore` (DynamoDB primary, file
fallback) is correct migration-phase engineering, but nothing ever
finishes the migration -- a tenant hand-added to tenants.yaml stays
file-only forever unless someone manually re-creates it in DynamoDB.
Two permanent sources of truth (not two migration-phase layers) is the
actual long-term risk plan section 35.13 names. This script is the
concrete tool for finishing the migration -- it does NOT decide FOR
you whether to actually retire tenants.yaml/FilePolicyStore afterward;
that's still a real decision (some tenants may be deliberately kept
git-reviewed forever, per that section's own note), just no longer a
blocked one.

Idempotent and safe to re-run: DynamoDbPolicyStore.create() is a
conditional write that fails (TenantAlreadyExistsError) if the tenant
already exists in DynamoDB, so an already-migrated tenant is silently
skipped, never overwritten. Dry-run by default -- --apply is required
to actually write.

Usage:
    python -m scripts.migrate_file_tenants_to_dynamodb \\
        --tenants-path policies/tenants.yaml \\
        --table-name gateway-tenant-policies-dev \\
        --region us-east-1
        [--apply]
"""
from __future__ import annotations

import argparse
import sys

from services.gateway.policy.models import TenantAlreadyExistsError
from services.gateway.policy.store import DynamoDbPolicyStore, load_policies_from_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenants-path", default="policies/tenants.yaml")
    parser.add_argument("--table-name", required=True, help="DynamoDB table (PROVISIONED_TENANT_POLICIES_TABLE_NAME)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this, only prints what would happen.")
    args = parser.parse_args()

    file_store = load_policies_from_yaml(args.tenants_path)
    tenant_ids = file_store.list_tenant_ids()
    if not tenant_ids:
        print(f"No tenants found in {args.tenants_path}.")
        return 0

    dynamo_store = DynamoDbPolicyStore(table_name=args.table_name, region=args.region)

    migrated, skipped, failed = [], [], []
    for tenant_id in sorted(tenant_ids):
        policy = file_store.get(tenant_id)
        if not args.apply:
            print(f"[dry-run] would migrate '{tenant_id}' (rpm_limit={policy.rpm_limit}, state={policy.state.value})")
            continue
        try:
            dynamo_store.create(policy)
            migrated.append(tenant_id)
            print(f"migrated '{tenant_id}'")
        except TenantAlreadyExistsError:
            skipped.append(tenant_id)
            print(f"skipped '{tenant_id}' -- already exists in DynamoDB")
        except Exception as exc:  # noqa: BLE001 -- report and continue, don't abort the whole batch on one bad row
            failed.append((tenant_id, str(exc)))
            print(f"FAILED '{tenant_id}': {exc}", file=sys.stderr)

    if not args.apply:
        print(f"\n{len(tenant_ids)} tenant(s) would be checked. Re-run with --apply to actually migrate.")
        return 0

    print(f"\n{len(migrated)} migrated, {len(skipped)} already present, {len(failed)} failed.")
    if failed:
        return 1

    print(
        "\nNext step, once you've confirmed these are correct in DynamoDB "
        "(e.g. via GET /v1/admin/tenants): tenants.yaml is now redundant for "
        "the migrated ids -- removing them from that file is a separate, "
        "deliberate decision (plan section 35.13), not automated by this "
        "script."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
