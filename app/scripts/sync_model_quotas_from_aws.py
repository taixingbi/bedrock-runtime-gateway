#!/usr/bin/env python3
"""Pulls each certified model's real AWS Bedrock account quota
(requests-per-minute) from AWS Service Quotas and writes it into
gateway-model-quotas-dev's "quota#<model_id>" row -- the source
routing/model_quota.py's ModelQuotaCache reads at request time, so
routing/router.py's CertifiedRouter can skip a model before attempting
a call Bedrock would throttle anyway, rather than only reacting after
the fact (the existing fallback-on-ThrottlingException path, which
this doesn't replace -- see model_quota.py's own docstring).

Not guessed, not hardcoded: Bedrock's own quotas vary per account
(adjustable quotas can be raised via a support request) and change
over time -- this always reads the live value via
service-quotas:ListServiceQuotas, never a literal number baked into
this repo.

Model-id-to-quota-name mapping: this gateway's model IDs
(certified_models.yaml) are either a "us."-prefixed cross-region
inference profile or a bare on-demand model ID -- Service Quotas names
each quota "{Cross-region|On-demand} model inference requests per
minute for <human model name>", and there's no API that maps a
model_id to that display name directly, so MODEL_QUOTA_NAMES below is
hand-maintained. Add an entry there before this script can sync a
newly-certified model -- an unmapped model is skipped with a warning,
not a hard failure, and (deliberately) so is a mapped model whose named
quota isn't found in this account/region -- see model_quota.py's own
"fails open on an unknown model" reasoning; a model this script has
never successfully synced just gets no quota gate, not a stale/wrong
one.

Idempotent and safe to re-run: writes only the "quota#<model_id>"
row's rpm_limit/quota_type/updated_at via update_item, never touching
the separate "ratelimit#model#<model_id>" row ModelQuotaLimiter's live
token bucket owns -- re-running this never resets an in-flight
rate-limit window. Dry-run by default -- --apply is required to
actually write.

sync_quotas() below is deliberately client-injectable and argv-free
(main() is the thin CLI wrapper that constructs real boto3 clients
from argv and calls it) -- moto (this repo's usual AWS-emulation test
tool, see every other scripts/*_script test) has no service-quotas
support, so tests inject a hand-written fake service-quotas client
instead; DynamoDB writes are still verified against real moto-emulated
DynamoDB, same as every other migration script here.

Usage:
    python -m scripts.sync_model_quotas_from_aws \\
        --table-name gateway-model-quotas-dev \\
        --region us-east-1 \\
        [--apply]
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Tuple

# model_id -> (Service Quotas display name, is cross-region inference profile)
MODEL_QUOTA_NAMES: Dict[str, Tuple[str, bool]] = {
    "us.amazon.nova-micro-v1:0": ("Amazon Nova Micro", True),
    "us.amazon.nova-lite-v1:0": ("Amazon Nova Lite", True),
    "us.amazon.nova-pro-v1:0": ("Amazon Nova Pro", True),
    "us.meta.llama3-3-70b-instruct-v1:0": ("Meta Llama 3.3 70B Instruct", True),
    "qwen.qwen3-32b-v1:0": ("Qwen3 32B V1", False),
}


def _fetch_all_quotas(sq_client: Any) -> List[dict]:
    quotas = []
    paginator = sq_client.get_paginator("list_service_quotas")
    for page in paginator.paginate(ServiceCode="bedrock"):
        quotas.extend(page["Quotas"])
    return quotas


def sync_quotas(
    *, sq_client: Any, dynamo_client: Any, table_name: str, apply: bool,
    model_quota_names: Dict[str, Tuple[str, bool]] = MODEL_QUOTA_NAMES,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Returns (synced_model_ids, [(model_id, quota_name_not_found), ...]).
    dynamo_client may be None when apply=False (dry-run never writes,
    so it's never called)."""
    quotas = _fetch_all_quotas(sq_client)
    by_name = {q["QuotaName"]: q["Value"] for q in quotas}

    synced: List[str] = []
    skipped: List[Tuple[str, str]] = []
    for model_id, (quota_name, is_cross_region) in model_quota_names.items():
        kind = "Cross-region" if is_cross_region else "On-demand"
        full_name = f"{kind} model inference requests per minute for {quota_name}"
        rpm_limit = by_name.get(full_name)
        if rpm_limit is None:
            skipped.append((model_id, full_name))
            print(f"SKIP '{model_id}': quota '{full_name}' not found in this account/region", file=sys.stderr)
            continue

        rpm_limit = int(rpm_limit)
        if not apply:
            print(f"[dry-run] would set '{model_id}' rpm_limit={rpm_limit} (from '{full_name}')")
            continue

        dynamo_client.update_item(
            TableName=table_name,
            Key={"pk": {"S": f"quota#{model_id}"}},
            UpdateExpression="SET rpm_limit = :rpm, quota_type = :qt, updated_at = :ua",
            ExpressionAttributeValues={
                ":rpm": {"N": str(rpm_limit)},
                ":qt": {"S": "cross_region" if is_cross_region else "on_demand"},
                ":ua": {"N": str(int(time.time()))},
            },
        )
        synced.append(model_id)
        print(f"synced '{model_id}' rpm_limit={rpm_limit}")

    return synced, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table-name", required=True, help="DynamoDB table (MODEL_QUOTAS_TABLE_NAME)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this, only prints what would happen.")
    args = parser.parse_args()

    import boto3

    sq_client = boto3.client("service-quotas", region_name=args.region)
    dynamo_client = boto3.client("dynamodb", region_name=args.region) if args.apply else None

    synced, skipped = sync_quotas(
        sq_client=sq_client, dynamo_client=dynamo_client, table_name=args.table_name, apply=args.apply,
    )

    if not args.apply:
        print(f"\n{len(MODEL_QUOTA_NAMES)} model(s) would be checked. Re-run with --apply to actually write.")
        return 0

    print(f"\n{len(synced)} synced, {len(skipped)} skipped (quota not found).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
