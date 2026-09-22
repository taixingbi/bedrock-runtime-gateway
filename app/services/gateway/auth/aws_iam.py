"""AWS IAM (SigV4) caller identity, verified upstream.

This module does **not** verify SigV4 signatures -- that's API Gateway's
`AWS_IAM` authorizer's job (see infra/modules/api_gateway). By the time a
request reaches this app on the IAM route, API Gateway has already
verified the caller's SigV4 signature and overwritten
`x-platform-principal-arn`/`x-platform-account-id` with its own verified
`$context.identity.*` values, discarding whatever the client sent. On
every other route those same headers are stripped entirely. Trusting
them here is therefore only as safe as that infra invariant -- see
auth/identity.py's module docstring.

All this module does is map a verified IAM principal ARN to the
tenant_id/application_id/roles it should have, the IAM-path equivalent of
what a JWT's claims already carry directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

import yaml
from opentelemetry import trace
from opentelemetry.propagate import inject

from .identity import AuthError

HEADER_PRINCIPAL_ARN = "x-platform-principal-arn"
HEADER_ACCOUNT_ID = "x-platform-account-id"

_tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class IamPrincipalGrant:
    tenant_id: str
    application_id: str
    roles: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResourceAuthzDecision:
    """Result of a resource/context-aware `/v1/authorize` call (plan
    section 35.16) -- distinct from IamPrincipalGrant, which is only
    ever the identity-resolution call's result."""

    allow: bool
    policy_id: str
    policy_version: int
    reason: str


class IamTenantResolver(Protocol):
    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        """Return the grant for a verified IAM principal ARN, or raise AuthError.

        `request_id`/`session_id` are optional and only meaningful to
        `HttpIamTenantResolver` (forwarded as the `x-request-id`/
        `x-session-id` headers so a decision logged by
        platform-authz-service can be correlated back to the request
        that triggered it) -- every in-process resolver ignores them."""
        ...

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        """M10 portal: every configured ARN pattern -> grant, for the
        Applications page. Not a live/authoritative list of *callers*
        (nothing observes who's actually calling), just the configured
        grants -- the JWT auth path has no equivalent registry at all
        (any application_id in a validly-signed token works), so this
        can only ever show the AWS_IAM/SigV4 side of the picture."""
        ...


class FileIamTenantResolver:
    """Loads `policies/iam_tenants.yaml`'s `iam_principals` map once at
    startup, the IAM-path equivalent of `FilePolicyStore`/`tenants.yaml`.

    Matching: an exact ARN match wins; otherwise a pattern ending in "*"
    matches any ARN sharing that prefix (for
    "arn:...:assumed-role/<role>/*" session-name wildcards, since an
    assumed-role ARN's last segment is the caller-chosen session name,
    not something we can enumerate in advance).

    Tolerant of a missing file (empty mapping, so every principal 403s
    with UNKNOWN_IAM_PRINCIPAL) rather than failing app startup -- not
    every environment uses the IAM auth path.
    """

    def __init__(self, path: str):
        self._exact: Dict[str, IamPrincipalGrant] = {}
        self._prefixes: Dict[str, IamPrincipalGrant] = {}

        if not path or not os.path.exists(path):
            return

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for arn_pattern, entry in (data.get("iam_principals") or {}).items():
            grant = IamPrincipalGrant(
                tenant_id=entry["tenant_id"],
                application_id=entry["application_id"],
                roles=list(entry.get("roles") or []),
            )
            if arn_pattern.endswith("*"):
                self._prefixes[arn_pattern[:-1]] = grant
            else:
                self._exact[arn_pattern] = grant

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        grant = self._exact.get(principal_arn)
        if grant is not None:
            return grant

        for prefix, candidate in self._prefixes.items():
            if principal_arn.startswith(prefix):
                return candidate

        raise AuthError(
            f"no tenant mapping for IAM principal '{principal_arn}'",
            code="UNKNOWN_IAM_PRINCIPAL",
        )

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        merged: Dict[str, IamPrincipalGrant] = dict(self._exact)
        merged.update({f"{prefix}*": grant for prefix, grant in self._prefixes.items()})
        return merged


class ProvisionedIamTenantResolver(IamTenantResolver, Protocol):
    """M11: an IamTenantResolver that also supports onboarding's
    create-only write -- `InMemoryIamTenantResolver` and
    `DynamoDbIamTenantResolver` both satisfy this; `FileIamTenantResolver`
    deliberately doesn't (existing hand-managed grants are never
    created through this path)."""

    def put_grant(self, principal_arn: str, grant: IamPrincipalGrant) -> None:
        """Raises PrincipalAlreadyMappedError on conflict."""
        ...


class PrincipalAlreadyMappedError(Exception):
    """M11: raised by put_grant() on a conditional-write conflict --
    the authoritative check, not a caller's own resolve()-then-
    put_grant(), which would still race under concurrent onboarding
    requests for the same ARN."""

    def __init__(self, principal_arn: str):
        super().__init__(f"principal '{principal_arn}' is already mapped")
        self.principal_arn = principal_arn


class InMemoryIamTenantResolver:
    """M11: in-memory ProvisionedIamTenantResolver, the AWS_IAM-path
    equivalent of policy/store.py's InMemoryPolicyStore -- what tests
    and any environment without a principal-mappings table configured
    get instead of DynamoDbIamTenantResolver."""

    def __init__(self) -> None:
        self._grants: Dict[str, IamPrincipalGrant] = {}

    def put_grant(self, principal_arn: str, grant: IamPrincipalGrant) -> None:
        if principal_arn in self._grants:
            raise PrincipalAlreadyMappedError(principal_arn)
        self._grants[principal_arn] = grant

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        grant = self._grants.get(principal_arn)
        if grant is None:
            raise AuthError(
                f"no tenant mapping for IAM principal '{principal_arn}'",
                code="UNKNOWN_IAM_PRINCIPAL",
            )
        return grant

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        return dict(self._grants)


class DynamoDbIamTenantResolver:
    """Real, durable principal-mapping storage (M11, plan section
    22.3) -- the AWS_IAM-path equivalent of policy/store.py's
    DynamoDbPolicyStore, for the exact same reason: a principal mapping
    *provisioned* through onboarding (onboarding/provisioning.py)
    shouldn't require a `policies/iam_tenants.yaml` PR to take effect.
    Used as a layer (`LayeredIamTenantResolver` below), never a
    replacement for `FileIamTenantResolver`.

    Exact-ARN lookups only -- no prefix/wildcard matching. An
    onboarding request names one specific principal ARN; the "*"
    session-name-wildcard case (FileIamTenantResolver's `_prefixes`)
    only makes sense for hand-authored, review-gated entries.
    """

    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put_grant(self, principal_arn: str, grant: IamPrincipalGrant) -> None:
        """Provisioning-only: fails if this ARN is already mapped here,
        so onboarding can never silently overwrite an existing
        provisioned grant. Does not protect against colliding with a
        *file*-configured ARN -- callers must check
        `LayeredIamTenantResolver` first."""
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item={
                    "principal_arn": principal_arn,
                    "tenant_id": grant.tenant_id,
                    "application_id": grant.application_id,
                    "roles": list(grant.roles),
                },
                ConditionExpression="attribute_not_exists(principal_arn)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise PrincipalAlreadyMappedError(principal_arn) from exc
            raise

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        response = self._table.get_item(Key={"principal_arn": principal_arn})
        item = response.get("Item")
        if item is None:
            raise AuthError(
                f"no tenant mapping for IAM principal '{principal_arn}'",
                code="UNKNOWN_IAM_PRINCIPAL",
            )
        return IamPrincipalGrant(
            tenant_id=item["tenant_id"],
            application_id=item["application_id"],
            roles=list(item.get("roles", [])),
        )

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        merged: Dict[str, IamPrincipalGrant] = {}
        kwargs: Dict[str, object] = {}
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                merged[item["principal_arn"]] = IamPrincipalGrant(
                    tenant_id=item["tenant_id"],
                    application_id=item["application_id"],
                    roles=list(item.get("roles", [])),
                )
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return merged


class LayeredIamTenantResolver:
    """`primary` (provisioned/DynamoDB) checked first, `fallback`
    (hand-configured/file) second -- additive, mirrors
    policy/store.py's LayeredPolicyStore. A given ARN can only ever be
    mapped in one layer (provisioning refuses to create a grant for an
    ARN the fallback already maps -- see onboarding/provisioning.py)."""

    def __init__(self, *, primary: ProvisionedIamTenantResolver, fallback: IamTenantResolver):
        self._primary = primary
        self._fallback = fallback

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        try:
            return self._primary.resolve(principal_arn, request_id=request_id, session_id=session_id)
        except AuthError:
            return self._fallback.resolve(principal_arn, request_id=request_id, session_id=session_id)

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        merged = dict(self._fallback.list_grants())
        merged.update(self._primary.list_grants())
        return merged


class HttpIamTenantResolver:
    """M12 (plan.md Section 5): delegates principal mapping to
    platform-authz-service's `POST /v1/authorize` instead of resolving
    it in-process. Satisfies the same `IamTenantResolver` Protocol
    every other resolver in this file does -- wired in at main.py as a
    straight swap for `LayeredIamTenantResolver` when
    `AUTHZ_SERVICE_URL` is configured, so no route handler or pipeline
    stage changes; this is the seam the Protocol already existed for.

    urllib.request, not a new HTTP client dependency -- same choice
    jwt_verifier.py's JwksVerifier already made for its own outbound
    call.

    `ca_cert_pem`, when set, pins TLS verification to exactly that CA
    instead of the system trust store -- authz-service's ALB cert is
    issued by a private CA (this repo's infra/ half's
    aws_acmpca_certificate_authority.internal), which no public trust
    store knows about, so the default `ssl` behavior would reject it.
    Empty means "use the system default" (plain HTTP in dev/tests, or
    an environment that hasn't set this up).

    `client_cert_pem`/`client_key_pem`, when both set, present this
    service's own mTLS client identity on every call -- plan section
    35's P1 hardening. Stage 1 only (2026-09-22): authz-service's ALB
    trust store exists but its listener's mutual_authentication is
    still `mode = "off"`, so presenting a cert here is harmless and
    unverified until that's flipped to "verify" -- see
    modules/authz_service's own comment on why that cutover is
    deliberately separate. Either both are empty (no client cert
    presented, today's default) or both are set together; one without
    the other would fail at `load_cert_chain` with a confusing OpenSSL
    error rather than a clear config mistake, so this only ever writes
    files when both are present.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 5.0,
        ca_cert_pem: str = "",
        client_cert_pem: str = "",
        client_key_pem: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._ssl_context = None
        if ca_cert_pem:
            import ssl

            self._ssl_context = ssl.create_default_context(cadata=ca_cert_pem)
            if client_cert_pem and client_key_pem:
                # ssl.SSLContext.load_cert_chain has no in-memory/string
                # form (unlike create_default_context's own cadata=
                # above) -- both PEMs have to land on disk somewhere it
                # can open them. A private temp dir this process owns
                # for its own lifetime, never written before this call,
                # key file mode 0600 -- about as close to "never really
                # at rest" as the stdlib ssl module allows.
                import tempfile
                from pathlib import Path

                cert_dir = Path(tempfile.mkdtemp(prefix="authz-mtls-"))
                cert_path = cert_dir / "client.crt"
                key_path = cert_dir / "client.key"
                cert_path.write_text(client_cert_pem)
                key_path.write_text(client_key_pem)
                key_path.chmod(0o600)
                self._ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    def _post_authorize(
        self,
        body: dict,
        *,
        span_name: str,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Shared HTTP mechanics for both `resolve()` (identity-only,
        Stage 1) and `check_resource_access()` (resource/context-aware,
        Stage 4d) -- same endpoint, same headers/tracing, different
        request body and result interpretation per caller."""
        import json
        import urllib.error
        import urllib.request

        with _tracer.start_as_current_span(span_name) as span:
            headers = {"content-type": "application/json"}
            if request_id:
                # Lets platform-authz-service's own decision log carry the
                # SAME request_id as this request's gateway.chat/gateway.access
                # lines, instead of minting an unrelated one -- see
                # authz-service's main.py, which honors this header.
                headers["x-request-id"] = request_id
            if session_id:
                headers["x-session-id"] = session_id
            # W3C traceparent -- lets authz-service's own span be a
            # CHILD of this one (same trace_id), not an unrelated trace.
            # inject() writes into whatever dict-like carrier it's given
            # using the process's configured propagator (W3C TraceContext
            # by default), no manual header formatting needed.
            inject(headers)
            request = urllib.request.Request(
                f"{self._base_url}/v1/authorize",
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout_s, context=self._ssl_context
                ) as resp:  # noqa: S310 (fixed internal URL, not user input)
                    data = json.loads(resp.read())
            except urllib.error.URLError as exc:
                span.set_attribute("error", str(exc))
                raise AuthError(
                    f"authz-service unavailable: {exc}", code="AUTHORIZATION_SERVICE_UNAVAILABLE"
                ) from exc
            span.set_attribute("authz.decision", data["decision"])
            return data

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        data = self._post_authorize(
            {"identity": {"subject": principal_arn, "auth_type": "aws_iam"}, "action": "llm.invoke"},
            span_name="authz.authorize",
            request_id=request_id,
            session_id=session_id,
        )
        if data["decision"] != "ALLOW":
            raise AuthError(
                f"no tenant mapping for IAM principal '{principal_arn}'", code="UNKNOWN_IAM_PRINCIPAL"
            )
        return IamPrincipalGrant(
            tenant_id=data["tenant_id"], application_id=data["application_id"], roles=list(data["roles"])
        )

    def check_resource_access(
        self,
        principal_arn: str,
        *,
        action: str,
        resource_id: str,
        resource_type: str = "model",
        context: Optional[Dict[str, object]] = None,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ResourceAuthzDecision:
        """Plan section 35.16 (P2 production hardening) -- closes the
        honest scope gap platform-authz-service's own policy_engine.py
        documents: the identity-resolution call above (`resolve()`,
        Stage 1) happens before the model is resolved, so it can never
        carry `resource`/`context`, and rules keyed on
        `denied_resource_ids`/`max_data_classification` never fired
        against real traffic. This is a SECOND call to the same
        `/v1/authorize` endpoint, made from pipeline.py's
        `enforce_resource_authorization` right after the model is
        resolved (Stage 4c), once `resource_id`/`context` actually
        exist. No new schema needed server-side -- `AuthorizeRequest`
        already had `resource`/`context` on the wire; the server just
        re-resolves the same principal_arn -> tenant_id/roles mapping
        `resolve()` already does (cheap, same lookup) and evaluates the
        PolicyEngine's rules with them populated this time.

        JWT-path identities have no principal_arn and are never routed
        here -- see pipeline.enforce_resource_authorization's own
        docstring for that scope boundary.
        """
        data = self._post_authorize(
            {
                "identity": {"subject": principal_arn, "auth_type": "aws_iam"},
                "action": action,
                "resource": {"type": resource_type, "id": resource_id},
                "context": context or {},
            },
            span_name="authz.check_resource_access",
            request_id=request_id,
            session_id=session_id,
        )
        return ResourceAuthzDecision(
            allow=data["decision"] == "ALLOW",
            policy_id=data["policy_id"],
            policy_version=data.get("policy_version", 1),
            reason=data["reason"],
        )

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        import json
        import urllib.request

        with urllib.request.urlopen(
            f"{self._base_url}/v1/grants", timeout=self._timeout_s, context=self._ssl_context
        ) as resp:  # noqa: S310
            data = json.loads(resp.read())
        return {
            arn: IamPrincipalGrant(
                tenant_id=g["tenant_id"], application_id=g["application_id"], roles=list(g["roles"])
            )
            for arn, g in data["grants"].items()
        }
