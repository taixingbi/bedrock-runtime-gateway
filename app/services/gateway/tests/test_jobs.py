import unittest

from starlette.testclient import TestClient

from ..auth.aws_iam import HEADER_ACCOUNT_ID, HEADER_PRINCIPAL_ARN, IamPrincipalGrant
from ..config import load_settings
from ..jobs.queue import InMemoryJobQueue
from ..jobs.store import InMemoryJobStore
from ..main import create_app
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient

_KNOWN_ARN = "arn:aws:iam::646821141010:role/team-a-ai-client"


def _client():
    settings = load_settings()
    fixture = get_auth_fixture()
    job_store = InMemoryJobStore()
    job_queue = InMemoryJobQueue()
    app = create_app(
        settings=settings,
        converse_client=FakeConverseClient(),
        token_verifier=fixture.verifier,
        job_store=job_store,
        job_queue=job_queue,
    )
    return TestClient(app), job_store, job_queue


def _auth_headers(**token_kwargs) -> dict:
    return auth_header(get_auth_fixture().token(**token_kwargs))


class JobsEndpointTests(unittest.TestCase):
    def test_submit_returns_202_and_enqueues_by_id(self):
        client, job_store, job_queue = _client()

        resp = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "QUEUED")
        self.assertTrue(body["job_id"])
        self.assertEqual(job_queue.sent, [body["job_id"]])

        stored = job_store.get(body["job_id"])
        self.assertEqual(stored.tenant_id, "finance")
        self.assertEqual(stored.application_id, "risk-chat")

    def test_get_returns_submitted_job_status(self):
        client, _, _ = _client()

        submit = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(),
        )
        job_id = submit.json()["job_id"]

        resp = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "QUEUED")

    def test_get_unknown_job_returns_404(self):
        client, _, _ = _client()

        resp = client.get("/v1/jobs/does-not-exist", headers=_auth_headers())

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "JOB_NOT_FOUND")

    def test_cannot_read_another_tenants_job(self):
        client, _, _ = _client()

        submit = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(tenant_id="finance"),
        )
        job_id = submit.json()["job_id"]

        # sandbox has an empty model allowlist -- unrelated to this check,
        # just a second real tenant in policies/tenants.yaml to submit as.
        resp = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers(tenant_id="sandbox"))

        self.assertEqual(resp.status_code, 404)

    def test_missing_auth_is_rejected_before_enqueueing(self):
        client, _, job_queue = _client()

        resp = client.post(
            "/v1/jobs", json={"messages": [{"role": "user", "content": "hi"}]}
        )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(job_queue.sent, [])


class _FakeIamResolverWithResourceAuthz:
    """Combines FileIamTenantResolver's `resolve()` Protocol with
    HttpIamTenantResolver's `check_resource_access()` (duck-typed, see
    pipeline.enforce_resource_authorization's docstring) -- records
    every call so a test can assert exactly what request_id/session_id
    actually reached it. Standing in for both in one fake since a real
    HttpIamTenantResolver would do both over the wire to the same
    platform-authz-service call."""

    def __init__(self, grant: IamPrincipalGrant):
        self._grant = grant
        self.resource_authz_calls = []

    def resolve(self, principal_arn: str, *, request_id=None, session_id=None) -> IamPrincipalGrant:
        return self._grant

    def check_resource_access(self, principal_arn, **kwargs):
        self.resource_authz_calls.append(kwargs)
        from ..auth.aws_iam import ResourceAuthzDecision

        return ResourceAuthzDecision(allow=True, policy_id="p", policy_version=1, reason="ok")


class JobsSessionIdPropagationTests(unittest.TestCase):
    """Regression test: submit_job()/get_job() used to build their own
    Identity via _authenticate(request) without threading request_id/
    session_id through (unlike routes.py's /v1/chat handler, which
    always has) -- silent drift, not a crash, since both are optional
    everywhere they're read. Caught during a repo-wide cleanup pass;
    this locks the fix in so it can't quietly regress again."""

    def _client(self):
        settings = load_settings()
        fixture = get_auth_fixture()
        resolver = _FakeIamResolverWithResourceAuthz(
            IamPrincipalGrant(tenant_id="team-a", application_id="team-a-ai-client", roles=["developer"])
        )
        app = create_app(
            settings=settings,
            converse_client=FakeConverseClient(),
            token_verifier=fixture.verifier,
            iam_tenant_resolver=resolver,
            job_store=InMemoryJobStore(),
            job_queue=InMemoryJobQueue(),
        )
        return TestClient(app), resolver

    def test_submit_job_forwards_session_id_to_resource_authorization(self):
        client, resolver = self._client()

        resp = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={
                HEADER_PRINCIPAL_ARN: _KNOWN_ARN,
                HEADER_ACCOUNT_ID: "646821141010",
                "x-session-id": "sess-abc-123",
            },
        )

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(resolver.resource_authz_calls), 1)
        self.assertEqual(resolver.resource_authz_calls[0]["session_id"], "sess-abc-123")


if __name__ == "__main__":
    unittest.main()
