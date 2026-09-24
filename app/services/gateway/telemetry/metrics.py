"""CloudWatch Embedded Metric Format (EMF) emission -- turns request
outcomes into real, queryable CloudWatch custom metrics with NO new AWS
service and NO new IAM permission: EMF-formatted JSON log lines are
extracted into metrics automatically, server-side, by CloudWatch Logs
itself -- the app already has `logs:PutLogEvents` (it's already
logging), so there's nothing new to grant. No client library either
(EMF is a documented JSON schema; hand-rolled here rather than adding
the aws-embedded-metrics package for four metric names).

One log line can declare MULTIPLE dimension sets at once -- every call
here emits both a per-tenant rollup (`["environment", "tenant_id"]`)
and a global-per-environment rollup (`["environment"]`) from the SAME
emission, so "global" and "per-tenant" dashboards/alarms both come
from one instrumentation point per request, not two. A `model`
dimension set is added too when a model is known, for per-model
breakdowns (e.g. which model is actually driving cost or errors).

`environment` is in EVERY dimension set, including the "global" one --
deliberately never a dimensionless rollup. This account/region has one
shared CloudWatch namespace across dev and (eventually) prod; without
`environment` as a real dimension, the moment prod carries any traffic
its metrics would silently blend into dev's global widgets/alarms (and
vice versa) with no way to tell them apart after the fact. Caught and
fixed before prod ever ran this code, not after.

Written directly to stdout as its own JSON line, deliberately NOT
through telemetry/logging.py's log_event/JsonFormatter pipeline: that
formatter drops every key starting with "_" (its generic filter for
Python's own internal LogRecord attributes), which would silently
strip EMF's required top-level `_aws` key -- caught live while writing
this module's own tests, not guessed. CloudWatch's EMF extractor reads
a plain JSON line from CloudWatch Logs; it doesn't need or want this
gateway's own ts/level/service/request_id fields riding along, so a
small dedicated writer is also simpler than special-casing the shared
formatter for one caller.

Metric semantics:
  - RequestCount: 1 per call, unconditionally -- the denominator for
    rate math (error rate = ErrorCount/RequestCount, etc.) done in
    CloudWatch metric math / dashboard widgets, not computed here.
  - TTFTMs: only meaningful for streaming responses (api/routes.py's
    stream_chunks) -- omitted (not zero) for non-streaming requests,
    since "time to first token" isn't a distinct concept from e2e
    latency when the whole response arrives in one shot.
  - E2ELatencyMs: every request that reached a terminal outcome,
    success or failure.
  - EstimatedCostUsd: success only -- matches telemetry/cost.py's own
    estimate_cost, which needs real post-call input/output token
    counts that only exist on success.
  - ErrorCount: an admitted request that failed downstream (Bedrock
    error, timeout, all-routes-unavailable) -- distinct from...
  - RejectCount: an admission-control stage rejected the request
    before it ever reached the model (kill_switch/rate_limit/
    token_rate_limit/budget/concurrency/model-quota) -- `reject_stage`
    rides along as a plain (non-dimension) attribute, not a fourth
    dimension set, to keep this project's metric cardinality low at
    its current tenant/model count.
"""
from __future__ import annotations

import json
import sys
import time
from typing import List, Optional

_NAMESPACE = "BedrockGateway"


def emit_request_metric(
    *,
    environment: str,
    tenant_id: str,
    model: Optional[str] = None,
    e2e_latency_ms: Optional[float] = None,
    ttft_ms: Optional[float] = None,
    estimated_cost_usd: Optional[float] = None,
    error: bool = False,
    reject_stage: Optional[str] = None,
) -> None:
    """Call once per request at its terminal outcome (success, error,
    or admission reject) -- see module docstring for exactly what each
    optional value means and when to pass it. `environment` has no
    default (every caller must say which environment it's running in,
    same as telemetry/logging.py's configure_logging) -- see module
    docstring for why this can never be allowed to default/fall back
    to a dimensionless rollup."""
    metric_defs = [{"Name": "RequestCount", "Unit": "Count"}]
    values = {"RequestCount": 1}

    if e2e_latency_ms is not None:
        metric_defs.append({"Name": "E2ELatencyMs", "Unit": "Milliseconds"})
        values["E2ELatencyMs"] = e2e_latency_ms
    if ttft_ms is not None:
        metric_defs.append({"Name": "TTFTMs", "Unit": "Milliseconds"})
        values["TTFTMs"] = ttft_ms
    if estimated_cost_usd is not None:
        metric_defs.append({"Name": "EstimatedCostUsd", "Unit": "None"})
        values["EstimatedCostUsd"] = estimated_cost_usd
    if error:
        metric_defs.append({"Name": "ErrorCount", "Unit": "Count"})
        values["ErrorCount"] = 1
    if reject_stage is not None:
        metric_defs.append({"Name": "RejectCount", "Unit": "Count"})
        values["RejectCount"] = 1

    dimension_sets: List[List[str]] = [["environment", "tenant_id"], ["environment"]]
    if model is not None:
        dimension_sets.append(["environment", "tenant_id", "model"])

    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {"Namespace": _NAMESPACE, "Dimensions": dimension_sets, "Metrics": metric_defs}
            ],
        },
        "environment": environment,
        "tenant_id": tenant_id,
        **values,
    }
    if model is not None:
        payload["model"] = model
    if reject_stage is not None:
        payload["reject_stage"] = reject_stage

    print(json.dumps(payload, default=str), file=sys.stdout, flush=True)
