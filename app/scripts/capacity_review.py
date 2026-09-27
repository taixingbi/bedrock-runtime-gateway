#!/usr/bin/env python3
"""Review eval-bedrock-runtime-benchmark capacity profiles against a snapshot
of this gateway's limits (see services/gateway/capacity_review.py):

    python scripts/capacity_review.py \\
        --gateway-config docs/capacity-review-limits.example.yaml \\
        ../../eval-bedrock-runtime-benchmark/results/run-all-<ts>/*/*-capacity-profile.yaml

Prints the findings as YAML (warn first). Exits 1 when any warn-level
finding exists, so it can gate a config review. Read-only: it never
modifies gateway config.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from services.gateway.capacity_review import diff  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profiles", nargs="+", help="capacity-profile.yaml files (schema_version >= 3)")
    parser.add_argument("--gateway-config", required=True, help="YAML snapshot of the gateway's current limits")
    args = parser.parse_args(argv)

    profiles = [yaml.safe_load(Path(p).read_text()) for p in args.profiles]
    gateway = yaml.safe_load(Path(args.gateway_config).read_text()) or {}

    result = diff(profiles, gateway)
    print(yaml.safe_dump(result.to_dict(), sort_keys=False, width=120))
    return 1 if result.to_dict()["summary"]["warn"] else 0


if __name__ == "__main__":
    sys.exit(main())
