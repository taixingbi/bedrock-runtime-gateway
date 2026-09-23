# Runtime ownership and accounting rollout

This change makes the HTTP runtime and worker use the same policy, guardrail,
and concurrency dependency builders. Worker configuration must supply the
DynamoDB policy/admission tables and the same managed guardrail ID/version.

## Deployment order

1. Apply the environment's Terraform changes first. They grant the worker
   policy reads, shared concurrency operations, managed guardrail checks, and
   SQS visibility renewal. Both task roles also need usage-table PutItem for
   invocation accounting markers.
2. Update the worker's task-definition environment from Terraform. The ECS
   service intentionally ignores task_definition changes; applying Terraform
   alone does not update a running service. Confirm the next deployed revision
   contains PROVISIONED_TENANT_POLICIES_TABLE_NAME,
   ADMISSION_CONTROL_TABLE_NAME, BEDROCK_GUARDRAIL_ID and
   BEDROCK_GUARDRAIL_VERSION. Keep concurrency settings identical to the API.
3. Drain old worker tasks before starting the new workers. Old code does not
   respect execution ownership. Let existing gateway operations drain during
   rollout; old leases use expires_at and remain vulnerable to native TTL.
4. Deploy the application revision and smoke-test chat, streaming and jobs.
   Include output-guardrail rejection, concurrency rejection, a job longer
   than the initial SQS visibility period, and a worker restart mid-job.

No deployment or Terraform apply is performed by the code changes themselves.

## Guarantees and limits

- Lease release is conditional and idempotent. New concurrency leases use
  lease_expires_at, which is deliberately not the table's native TTL field.
  Reconciliation atomically compensates counters; it can also read legacy
  expires_at leases. Active work renews its lease. HTTP timeout/cancellation
  does not free a slot until the underlying blocking call finishes.
- A network partition can prevent renewal while a remote inference continues.
  This cannot forcibly fence Bedrock itself. SDK timeouts and monitoring of
  renewal failures remain necessary; this is not an exactly-once external API.
- Jobs are claimed atomically, active owners renew, and terminal writes require
  the same execution ID. Active RUNNING jobs are not acknowledged by duplicate
  consumers. SQS visibility is renewed during processing. A crashed attempt
  can be retried after lease expiry, so external inference may repeat.
- Provider estimates are recorded atomically across tenant monthly, tenant
  daily and application monthly totals, with a durable invocation marker.
  Repeating the same ledger write does not increment again. Response-cache
  hits incur zero inference spend; output rejection still records spend.
- A process crash after Bedrock responds but before the ledger commits can
  still lose usage. Recovery across that boundary requires a durable result/
  billing reconciliation mechanism. These estimates are not an AWS invoice.
- Streaming keeps its lease through consumption and close, offloads blocking
  reads, and writes an audit on completion/error/disconnect. Early disconnects
  may have no final provider token metadata; unknown usage remains null,
  never fabricated as zero. Streaming still has no output moderation, cache,
  or fallback; those pre-existing product limitations are not changed here.
- Authenticated chat terminal paths share one audit finalizer. Abrupt process
  death and unavailable audit storage are not an exactly-once durable delivery
  guarantee. Late provider completion after an HTTP timeout records usage but
  does not retroactively modify the HTTP audit outcome.

## Verification

Unit and integration regression tests cover duplicate release, stale
reconciliation, renewal, ownership fencing, cancellation, saturation,
DynamoDB-only policies, managed-guardrail selection, response-cache cost,
output-block cost, streaming capacity, and all three spend aggregates.
Tests use their own policy fixture rather than deployment YAML.
