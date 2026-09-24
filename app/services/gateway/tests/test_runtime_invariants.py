"""Regression coverage for shared capacity, ownership and provider accounting."""
import asyncio
import dataclasses
import json
import threading
import time
import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws
from starlette.testclient import TestClient

from ..concurrency import BlockingCallRunner, BlockingCallTimeoutError, ConcurrencyLimiter, DynamoDbConcurrencyLimiter, maintained_lease
from ..config import load_settings
from ..dependencies import build_policy_store, build_guardrail_client
from ..jobs.models import JobBusyError, JobStatus
from ..jobs.store import DynamoDbJobStore, InMemoryJobStore
from ..main import create_app
from ..policy.models import TenantPolicy
from ..policy.store import InMemoryPolicyStore, DynamoDbPolicyStore
from ..telemetry.request_audit import InMemoryRequestAuditStore
from ..usage.store import DynamoDbUsageStore, InMemoryUsageStore, record_provider_usage, current_month, current_day, get_application
from .auth_fixtures import get_auth_fixture, auth_header
from .fakes import FakeConverseClient
from .test_job_processor import _job, _process, _policy_cache, _router
from ..guardrails.basic_guardrail import BasicGuardrailClient


@mock_aws
class DistributedInvariantsTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client('dynamodb', region_name='us-east-1')
        self.client.create_table(TableName='capacity', KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}],
                                 AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
        self.limiter = DynamoDbConcurrencyLimiter(table_name='capacity', region='us-east-1', global_max=10,
                                                  default_tenant_max=10, client=self.client)
        self.limiter._RECONCILE_PROBABILITY = 0

    def test_duplicate_release_preserves_other_live_lease(self):
        a = self.limiter.try_acquire('a')
        b = self.limiter.try_acquire('a')
        self.limiter.release('a', a)
        self.limiter.release('a', a)
        self.assertEqual(self.limiter.current_global_count(), 1)
        self.limiter.release('a', b)
        self.assertEqual(self.limiter.current_global_count(), 0)

    def test_reconcile_then_release_preserves_new_work(self):
        a = self.limiter.try_acquire('a')
        self.limiter.reconcile(now=time.time() + 1000)
        self.limiter.try_acquire('a')
        self.limiter.release('a', a)
        self.assertEqual(self.limiter.current_global_count(), 1)

    def test_renewed_lease_is_not_reclaimed_from_stale_scan(self):
        a = self.limiter.try_acquire('a')
        snapshot = self.client.scan(TableName='capacity')['Items']
        leases = [item for item in snapshot if item['pk']['S'].startswith('lease#')]
        self.limiter.renew('a', a)
        with patch.object(self.client, 'scan', return_value={'Items': leases}):
            self.assertEqual(self.limiter.reconcile(now=time.time() - 1), 0)
        self.assertEqual(self.limiter.current_global_count(), 1)

    def test_new_lease_has_no_native_ttl_attribute(self):
        a = self.limiter.try_acquire('a')
        item = self.client.get_item(TableName='capacity', Key={'pk': {'S': f'lease#a#{a}'}})['Item']
        self.assertNotIn('expires_at', item)
        self.assertIn('lease_expires_at', item)

    def test_zero_cap_rejects_before_first_counter_exists(self):
        self.assertIsNone(self.limiter.try_acquire('zero', tenant_max=0))

    def test_dynamodb_accounting_is_atomic_and_idempotent(self):
        self.client.create_table(TableName='usage', KeySchema=[{'AttributeName': 'tenant_id', 'KeyType': 'HASH'},
            {'AttributeName': 'month', 'KeyType': 'RANGE'}], AttributeDefinitions=[{'AttributeName': 'tenant_id', 'AttributeType': 'S'},
            {'AttributeName': 'month', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
        store = DynamoDbUsageStore(table_name='usage', region='us-east-1')
        for _ in range(2):
            record_provider_usage(store, 'a', 'app', 0.5, invocation_id='one')
        self.assertEqual(store.get('a', current_month()), 0.5)
        self.assertEqual(store.get('a', current_day()), 0.5)
        self.assertEqual(get_application(store, 'a', 'app', current_month()), 0.5)

    def test_job_claim_excludes_other_owner_and_fences_stale_completion(self):
        self.client.create_table(TableName='jobs', KeySchema=[{'AttributeName': 'job_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'job_id', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
        store = DynamoDbJobStore(table_name='jobs', region='us-east-1')
        store.put(_job())
        first = store.claim('job-1', lease_s=1)
        with self.assertRaises(JobBusyError):
            store.claim('job-1')
        with patch('services.gateway.jobs.store.time.time', return_value=time.time() + 2):
            second = store.claim('job-1')
        with self.assertRaises(Exception):
            store.finish(dataclasses.replace(first, status=JobStatus.SUCCEEDED))
        store.finish(dataclasses.replace(second, status=JobStatus.SUCCEEDED))
        self.assertEqual(store.get('job-1').status, JobStatus.SUCCEEDED)

    def test_worker_policy_builder_reads_dynamodb_without_file_entries(self):
        import tempfile
        from pathlib import Path
        self.client.create_table(TableName='policies', KeySchema=[{'AttributeName': 'tenant_id', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'tenant_id', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
        store = DynamoDbPolicyStore(table_name='policies', region='us-east-1')
        store.create(TenantPolicy(tenant_id='new-tenant'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tenants.yaml'
            path.write_text('tenants: {}\n')
            settings = dataclasses.replace(load_settings(), tenant_policy_path=str(path),
                provisioned_tenant_policies_table_name='policies', aws_region='us-east-1')
            self.assertEqual(build_policy_store(settings).get('new-tenant').tenant_id, 'new-tenant')


class LifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_release_running_operation(self):
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=1)
        runner = BlockingCallRunner(max_workers=1, default_timeout_s=0.05)
        started, finish = threading.Event(), threading.Event()
        def work():
            token = limiter.try_acquire('a')
            with maintained_lease(limiter, 'a', token):
                started.set()
                finish.wait(2)
        try:
            with self.assertRaises(BlockingCallTimeoutError):
                await runner.run(work)
            self.assertTrue(started.is_set())
            self.assertEqual(limiter.current_global_count(), 1)
        finally:
            finish.set()
            await asyncio.to_thread(runner._executor.shutdown)
        self.assertEqual(limiter.current_global_count(), 0)

    async def test_stream_next_does_not_block_event_loop(self):
        from ..streaming import stream_chat_response
        from ..inference.bedrock_client import StreamChunk
        from ..routing.circuit_breaker import CircuitBreaker
        def chunks():
            time.sleep(0.1)
            yield StreamChunk(text_delta='hi')
        progressed = []
        async def ticker():
            await asyncio.sleep(0.01)
            progressed.append(True)
        async def connected():
            return False
        async def collect():
            async for _ in stream_chat_response(chunks(), model_id='m', request_id='r', tenant_id='a',
                    circuit_breaker=CircuitBreaker(), is_disconnected=connected):
                self.assertTrue(progressed)
        await asyncio.gather(collect(), ticker())


class AccountingAndAuditTests(unittest.TestCase):
    def make_client(self, fake, **overrides):
        self.usage = InMemoryUsageStore()
        self.audit = InMemoryRequestAuditStore()
        self.limiter = ConcurrencyLimiter(global_max=2, default_tenant_max=2)
        fixture = get_auth_fixture()
        self.headers = auth_header(fixture.token(tenant_id='acme'))
        return TestClient(create_app(settings=load_settings(), converse_client=fake,
            token_verifier=fixture.verifier, policy_store=InMemoryPolicyStore({'acme': TenantPolicy(tenant_id='acme')}),
            usage_store=self.usage, request_audit_store=self.audit, concurrency_limiter=self.limiter, **overrides))

    def test_response_cache_does_not_charge_provider_twice(self):
        fake = FakeConverseClient(input_tokens=1000, output_tokens=1000)
        client = self.make_client(fake)
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        self.assertEqual(client.post('/v1/chat', json=body, headers=self.headers).status_code, 200)
        first = self.usage.get('acme', current_month())
        response = client.post('/v1/chat', json=body, headers=self.headers)
        self.assertTrue(response.json()['cache_hit'])
        self.assertEqual(self.usage.get('acme', current_month()), first)
        self.assertEqual(len(self.audit.events), 2)
        self.assertEqual(self.audit.events[-1].estimated_cost, 0)

    def test_output_block_still_records_provider_cost_and_one_audit(self):
        client = self.make_client(FakeConverseClient(response_text='someone@example.com', input_tokens=1000, output_tokens=1000))
        response = client.post('/v1/chat', json={'messages': [{'role': 'user', 'content': 'hi'}]}, headers=self.headers)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertGreater(self.usage.get('acme', current_month()), 0)
        self.assertEqual(len(self.audit.events), 1)
        self.assertEqual(self.audit.events[0].guardrail_action, 'BLOCK')

    def test_stream_holds_capacity_and_accounts_once(self):
        outer = self
        class Observed(FakeConverseClient):
            def converse_stream(self, **kwargs):
                for chunk in super().converse_stream(**kwargs):
                    outer.assertEqual(outer.limiter.current_global_count(), 1)
                    yield chunk
        client = self.make_client(Observed(stream_chunks=['hello'], input_tokens=1000, output_tokens=1000))
        response = client.post('/v1/chat', json={'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertGreater(self.usage.get('acme', current_month()), 0)
        self.assertEqual(len(self.audit.events), 1)
        self.assertEqual(self.limiter.current_global_count(), 0)

    def test_worker_output_block_records_all_usage_dimensions(self):
        store = InMemoryJobStore()
        job = _job()
        store.put(job)
        usage = InMemoryUsageStore()
        _process(json.dumps({'job_id': job.job_id}), job_store=store,
            policy_cache=_policy_cache(finance=TenantPolicy(tenant_id='finance')),
            guardrail_client=BasicGuardrailClient(),
            router=_router(FakeConverseClient(response_text='somebody@example.com', input_tokens=1000, output_tokens=1000)),
            usage_store=usage)
        self.assertEqual(store.get(job.job_id).status, JobStatus.FAILED)
        amount = usage.get('finance', current_month())
        self.assertGreater(amount, 0)
        self.assertEqual(usage.get('finance', current_day()), amount)
        self.assertEqual(get_application(usage, 'finance', job.application_id, current_month()), amount)


class WorkerOwnershipTests(unittest.TestCase):
    def test_busy_job_does_not_acknowledge_message(self):
        from ...worker.main import run_forever
        class Queue:
            deleted = False
            heartbeats = 0
            def receive_message(self, **kwargs):
                return {'Messages': [{'Body': '{}', 'ReceiptHandle': 'receipt'}]}
            def change_message_visibility(self, **kwargs):
                self.heartbeats += 1
            def delete_message(self, **kwargs):
                self.deleted = True
        queue = Queue()
        iterations = iter([True, False])
        def busy(*args, **kwargs):
            raise JobBusyError('another worker owns this job')
        run_forever(sqs_client=queue, queue_url='q', process=busy, should_continue=lambda: next(iterations))
        self.assertFalse(queue.deleted)
        self.assertEqual(queue.heartbeats, 1)

    def test_worker_saturation_requeues_without_calling_provider(self):
        from ..jobs.processor import process_one
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=1)
        limiter.try_acquire('other')
        store = InMemoryJobStore()
        store.put(_job())
        fake = FakeConverseClient()
        with self.assertRaises(JobBusyError):
            process_one('{"job_id":"job-1"}', environment='dev', job_store=store,
                policy_cache=_policy_cache(finance=TenantPolicy(tenant_id='finance')),
                guardrail_client=BasicGuardrailClient(), router=_router(fake),
                usage_store=InMemoryUsageStore(), concurrency_limiter=limiter)
        self.assertEqual(store.get('job-1').status, JobStatus.QUEUED)
        self.assertEqual(fake.calls, [])

    def test_worker_selects_configured_managed_guardrail(self):
        settings = dataclasses.replace(load_settings(), bedrock_guardrail_id='configured', bedrock_guardrail_version='2')
        with patch('services.gateway.dependencies.BedrockGuardrailClient') as client:
            self.assertIs(build_guardrail_client(settings), client.return_value)
            self.assertEqual(client.call_args.kwargs['guardrail_id'], 'configured')


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_stream_keeps_capacity_until_pending_read_finishes(self):
        from ..streaming import stream_chat_response
        from ..inference.bedrock_client import StreamChunk
        from ..routing.circuit_breaker import CircuitBreaker
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=1)
        started, finish = threading.Event(), threading.Event()
        completions = []
        def chunks():
            token = limiter.try_acquire('a')
            with maintained_lease(limiter, 'a', token):
                started.set()
                finish.wait(2)
                yield StreamChunk(text_delta='hi')
        async def connected():
            return False
        async def consume():
            async for _ in stream_chat_response(chunks(), model_id='m', tenant_id='a', request_id='r',
                    circuit_breaker=CircuitBreaker(), is_disconnected=connected,
                    on_complete=lambda final, status, ttft_ms, duration_ms: completions.append(status)):
                pass
        task = asyncio.create_task(consume())
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertEqual(limiter.current_global_count(), 1)
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(limiter.current_global_count(), 0)
        self.assertEqual(completions, [499])

    async def test_timed_out_executor_rejects_more_work_until_thread_finishes(self):
        from ..concurrency import BlockingCallCapacityError
        runner = BlockingCallRunner(max_workers=1, default_timeout_s=0.02)
        finish = threading.Event()
        try:
            with self.assertRaises(BlockingCallTimeoutError):
                await runner.run(finish.wait, 2)
            with self.assertRaises(BlockingCallCapacityError):
                await runner.run(lambda: None)
        finally:
            finish.set()
            await asyncio.to_thread(runner._executor.shutdown)
