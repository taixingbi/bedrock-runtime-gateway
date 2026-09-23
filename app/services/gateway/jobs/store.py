"""Job record storage (M7). `JobStore` is the seam -- same pattern as
`PolicyStore`/`ResponseCache`/`ConverseClient`. `InMemoryJobStore` is what
tests and any environment without JOBS_TABLE_NAME configured get;
`DynamoDbJobStore` is the real backend, wired in by main.py/worker/main.py
when settings.jobs_table_name is set.
"""
from __future__ import annotations

import threading
import dataclasses
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Protocol

from .models import Job, JobMessage, JobNotFoundError, JobStatus, JobBusyError


class JobStore(Protocol):
    def put(self, job: Job) -> None:
        """Create or fully overwrite a job record."""
        ...

    def get(self, job_id: str) -> Job:
        """Raises JobNotFoundError if job_id doesn't exist."""
        ...

    def claim(self, job_id: str, lease_s: float = 120) -> Job: ...

    def renew(self, job: Job, lease_s: float = 120) -> None: ...

    def finish(self, job: Job) -> None: ...


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def put(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Job:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise JobNotFoundError(job_id) from None

    def claim(self, job_id: str, lease_s: float = 120) -> Job:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError(job_id)
            job = self._jobs[job_id]
            if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                return job
            if job.status == JobStatus.RUNNING and (job.lease_expires_at or 0) > time.time():
                raise JobBusyError(job_id)
            job = dataclasses.replace(job, status=JobStatus.RUNNING,
                                      execution_id=str(uuid.uuid4()), lease_expires_at=time.time() + lease_s)
            self._jobs[job_id] = job
            return job

    def renew(self, job: Job, lease_s: float = 120) -> None:
        with self._lock:
            current = self._jobs[job.job_id]
            if current.status != JobStatus.RUNNING or current.execution_id != job.execution_id:
                raise JobBusyError(job.job_id)
            self._jobs[job.job_id] = dataclasses.replace(current, lease_expires_at=time.time() + lease_s)

    def finish(self, job: Job) -> None:
        with self._lock:
            current = self._jobs[job.job_id]
            if current.status != JobStatus.RUNNING or current.execution_id != job.execution_id:
                raise JobBusyError(job.job_id)
            self._jobs[job.job_id] = job


def _job_to_item(job: Job) -> Dict[str, Any]:
    # DynamoDB's Number type rejects Python float directly (boto3 raises
    # "Float types are not supported. Use Decimal types instead.") --
    # only temperature/created_at need this, max_tokens/usage_* are ints.
    item: Dict[str, Any] = {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "application_id": job.application_id,
        "status": job.status.value,
        "model": job.model,
        "messages": [{"role": m.role, "content": m.content} for m in job.messages],
        "max_tokens": job.max_tokens,
        "temperature": Decimal(str(job.temperature)),
        "created_at": Decimal(str(job.created_at)),
    }
    for key, value in (
        ("execution_id", job.execution_id),
        ("lease_expires_at", Decimal(str(job.lease_expires_at)) if job.lease_expires_at is not None else None),
        ("output", job.output),
        ("error_code", job.error_code),
        ("error_message", job.error_message),
        ("usage_input_tokens", job.usage_input_tokens),
        ("usage_output_tokens", job.usage_output_tokens),
    ):
        if value is not None:
            item[key] = value
    return item


def _item_to_job(item: Dict[str, Any]) -> Job:
    return Job(
        job_id=item["job_id"],
        tenant_id=item["tenant_id"],
        application_id=item["application_id"],
        status=JobStatus(item["status"]),
        model=item["model"],
        messages=[JobMessage(role=m["role"], content=m["content"]) for m in item.get("messages", [])],
        max_tokens=int(item["max_tokens"]),
        temperature=float(item["temperature"]),
        created_at=float(item["created_at"]),
        execution_id=item.get("execution_id"),
        lease_expires_at=float(item["lease_expires_at"]) if "lease_expires_at" in item else None,
        output=item.get("output"),
        error_code=item.get("error_code"),
        error_message=item.get("error_message"),
        usage_input_tokens=int(item["usage_input_tokens"]) if "usage_input_tokens" in item else None,
        usage_output_tokens=int(item["usage_output_tokens"]) if "usage_output_tokens" in item else None,
    )


class DynamoDbJobStore:
    """Real DynamoDB-backed JobStore. boto3 imported lazily (only when
    actually constructed) so this module stays importable without boto3
    installed -- same reasoning as inference/bedrock_client.py's
    BedrockClient."""

    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, job: Job) -> None:
        self._table.put_item(Item=_job_to_item(job))

    def get(self, job_id: str) -> Job:
        response = self._table.get_item(Key={"job_id": job_id}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            raise JobNotFoundError(job_id)
        return _item_to_job(item)


    def claim(self, job_id: str, lease_s: float = 120) -> Job:
        from botocore.exceptions import ClientError
        try:
            result = self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #status = :running, execution_id = :owner, lease_expires_at = :expiry",
                ConditionExpression="attribute_exists(job_id) AND (#status = :queued OR "
                    "(#status = :running AND (attribute_not_exists(lease_expires_at) OR lease_expires_at < :now)))",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":running": "RUNNING", ":queued": "QUEUED",
                    ":owner": str(uuid.uuid4()), ":expiry": Decimal(str(time.time() + lease_s)),
                    ":now": Decimal(str(time.time()))},
                ReturnValues="ALL_NEW",
            )
            return _item_to_job(result["Attributes"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            job = self.get(job_id)
            if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                return job
            raise JobBusyError(job_id) from exc

    def renew(self, job: Job, lease_s: float = 120) -> None:
        self._table.update_item(
            Key={"job_id": job.job_id},
            UpdateExpression="SET lease_expires_at = :expiry",
            ConditionExpression="execution_id = :owner AND #status = :running",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":owner": job.execution_id, ":running": "RUNNING",
                ":expiry": Decimal(str(time.time() + lease_s))},
        )

    def finish(self, job: Job) -> None:
        self._table.put_item(
            Item=_job_to_item(job),
            ConditionExpression="execution_id = :owner AND #status = :running",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":owner": job.execution_id, ":running": "RUNNING"},
        )
