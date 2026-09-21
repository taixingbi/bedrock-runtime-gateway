"""Plan section 34.5 (model registry) + 34.4b (data-classification
model-eligibility gate). Loader/lookup tests here; pipeline
integration (enforce_model_certification's governance checks) in
PipelineModelGovernanceTests below.
"""
import tempfile
import unittest
from pathlib import Path

from .. import pipeline
from ..routing.model_registry import (
    ModelRegistryEntry,
    ModelStatus,
    classification_rank,
    get_status,
    load_model_registry_from_yaml,
)


class ClassificationRankTests(unittest.TestCase):
    def test_known_values_are_ordered(self):
        self.assertLess(classification_rank("public"), classification_rank("internal"))
        self.assertLess(classification_rank("internal"), classification_rank("confidential"))
        self.assertLess(classification_rank("confidential"), classification_rank("phi"))

    def test_case_insensitive(self):
        self.assertEqual(classification_rank("PHI"), classification_rank("phi"))

    def test_pii_and_phi_are_equal_rank(self):
        self.assertEqual(classification_rank("pii"), classification_rank("phi"))

    def test_unknown_value_is_none(self):
        self.assertIsNone(classification_rank("top-secret"))

    def test_none_is_none(self):
        self.assertIsNone(classification_rank(None))


class GetStatusTests(unittest.TestCase):
    def test_absent_model_is_approved(self):
        self.assertEqual(get_status("some-model", registry={}), ModelStatus.APPROVED)

    def test_present_model_uses_registry_status(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.BLOCKED, owner="x", risk_classification="HIGH",
                approved_use_cases=[],
            )
        }
        self.assertEqual(get_status("m1", registry=registry), ModelStatus.BLOCKED)


class LoadModelRegistryFromYamlTests(unittest.TestCase):
    def test_missing_file_is_empty_registry(self):
        self.assertEqual(load_model_registry_from_yaml("/no/such/file.yaml"), {})

    def test_empty_path_is_empty_registry(self):
        self.assertEqual(load_model_registry_from_yaml(""), {})

    def test_loads_full_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_registry.yaml"
            path.write_text(
                """
model_registry:
  my-model:
    status: CONDITIONAL
    owner: ml-team
    risk_classification: MODERATE
    approved_use_cases: [chat]
    region: us-east-1
    max_data_classification: INTERNAL
    model_version: "1.0"
    retirement_date: "2027-01-01"
    notes: "under review"
"""
            )
            registry = load_model_registry_from_yaml(str(path))

            entry = registry["my-model"]
            self.assertEqual(entry.status, ModelStatus.CONDITIONAL)
            self.assertEqual(entry.owner, "ml-team")
            self.assertEqual(entry.max_data_classification, "INTERNAL")
            self.assertEqual(entry.retirement_date, "2027-01-01")


class PipelineModelGovernanceTests(unittest.TestCase):
    """pipeline.enforce_model_certification's registry/classification
    checks -- run only after the base certified_model_ids check, and
    only when model_registry is supplied (backward compatible with
    every pre-34.5 call site)."""

    def test_no_registry_supplied_skips_governance_entirely(self):
        result = pipeline.enforce_model_certification("m1", certified_model_ids={"m1"})
        self.assertIsNone(result)

    def test_uncertified_model_still_rejected_before_registry_checked(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification("m1", certified_model_ids=set(), model_registry={})
        self.assertEqual(ctx.exception.code, "MODEL_NOT_CERTIFIED")

    def test_model_absent_from_registry_is_allowed(self):
        result = pipeline.enforce_model_certification("m1", certified_model_ids={"m1"}, model_registry={})
        self.assertIsNone(result)

    def test_blocked_model_rejected(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.BLOCKED, owner="x", risk_classification="HIGH",
                approved_use_cases=[],
            )
        }
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification("m1", certified_model_ids={"m1"}, model_registry=registry)
        self.assertEqual(ctx.exception.code, "MODEL_NOT_APPROVED")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_deprecated_model_rejected(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.DEPRECATED, owner="x", risk_classification="LOW",
                approved_use_cases=[],
            )
        }
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification("m1", certified_model_ids={"m1"}, model_registry=registry)
        self.assertEqual(ctx.exception.code, "MODEL_NOT_APPROVED")

    def test_conditional_model_allowed_with_warning_returned(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.CONDITIONAL, owner="x", risk_classification="MODERATE",
                approved_use_cases=[], notes="pending review",
            )
        }
        result = pipeline.enforce_model_certification("m1", certified_model_ids={"m1"}, model_registry=registry)
        self.assertIsNotNone(result)
        self.assertIn("pending review", result)

    def test_approved_model_no_warning(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[],
            )
        }
        result = pipeline.enforce_model_certification("m1", certified_model_ids={"m1"}, model_registry=registry)
        self.assertIsNone(result)

    def test_tenant_classification_exceeds_model_limit_rejected(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="internal",
            )
        }
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification(
                "m1", certified_model_ids={"m1"}, model_registry=registry,
                tenant_data_classification="phi",
            )
        self.assertEqual(ctx.exception.code, "DATA_CLASSIFICATION_EXCEEDS_MODEL_LIMIT")

    def test_tenant_classification_within_model_limit_allowed(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="phi",
            )
        }
        result = pipeline.enforce_model_certification(
            "m1", certified_model_ids={"m1"}, model_registry=registry,
            tenant_data_classification="internal",
        )
        self.assertIsNone(result)

    def test_unresolvable_classification_skips_check_rather_than_blocking(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="top-secret-unknown-value",
            )
        }
        result = pipeline.enforce_model_certification(
            "m1", certified_model_ids={"m1"}, model_registry=registry,
            tenant_data_classification="phi",
        )
        self.assertIsNone(result)

    def test_no_tenant_classification_skips_check(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="internal",
            )
        }
        result = pipeline.enforce_model_certification(
            "m1", certified_model_ids={"m1"}, model_registry=registry, tenant_data_classification=None,
        )
        self.assertIsNone(result)


class PipelineModelGovernanceFailClosedTests(unittest.TestCase):
    """Plan section 35.3 (P0 production hardening): fail_closed=True
    flips two defaults from permissive to restrictive. Off by default
    -- every test above this class runs with fail_closed unset
    (False), confirming pre-35.3 behavior is unchanged."""

    def test_default_is_fail_open_unchanged(self):
        result = pipeline.enforce_model_certification(
            "unregistered-model", certified_model_ids={"unregistered-model"}, model_registry={},
        )
        self.assertIsNone(result)

    def test_model_absent_from_registry_is_blocked(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification(
                "unregistered-model", certified_model_ids={"unregistered-model"}, model_registry={},
                fail_closed=True,
            )
        self.assertEqual(ctx.exception.code, "MODEL_NOT_APPROVED")

    def test_model_present_and_approved_still_allowed(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[],
            )
        }
        result = pipeline.enforce_model_certification(
            "m1", certified_model_ids={"m1"}, model_registry=registry, fail_closed=True,
        )
        self.assertIsNone(result)

    def test_unresolvable_model_classification_rejected(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="top-secret-unknown-value",
            )
        }
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification(
                "m1", certified_model_ids={"m1"}, model_registry=registry,
                tenant_data_classification="phi", fail_closed=True,
            )
        self.assertEqual(ctx.exception.code, "MODEL_DATA_CLASSIFICATION_UNRESOLVABLE")

    def test_unresolvable_tenant_classification_rejected(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="internal",
            )
        }
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification(
                "m1", certified_model_ids={"m1"}, model_registry=registry,
                tenant_data_classification="not-a-real-classification", fail_closed=True,
            )
        self.assertEqual(ctx.exception.code, "TENANT_DATA_CLASSIFICATION_UNRESOLVABLE")

    def test_tenant_with_no_classification_at_all_still_allowed(self):
        """Not opting into data_classification at all is a legitimate
        no-requirement case in both modes -- distinct from an
        unresolvable (typo'd) value, which fail_closed rejects."""
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="internal",
            )
        }
        result = pipeline.enforce_model_certification(
            "m1", certified_model_ids={"m1"}, model_registry=registry,
            tenant_data_classification=None, fail_closed=True,
        )
        self.assertIsNone(result)

    def test_resolvable_classification_check_still_enforced_under_fail_closed(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.APPROVED, owner="x", risk_classification="LOW",
                approved_use_cases=[], max_data_classification="internal",
            )
        }
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_model_certification(
                "m1", certified_model_ids={"m1"}, model_registry=registry,
                tenant_data_classification="phi", fail_closed=True,
            )
        self.assertEqual(ctx.exception.code, "DATA_CLASSIFICATION_EXCEEDS_MODEL_LIMIT")


class GetStatusFailClosedTests(unittest.TestCase):
    def test_default_fail_open(self):
        self.assertEqual(get_status("absent-model", registry={}), ModelStatus.APPROVED)

    def test_fail_closed_blocks_absent_model(self):
        self.assertEqual(get_status("absent-model", registry={}, fail_closed=True), ModelStatus.BLOCKED)

    def test_fail_closed_does_not_override_a_present_entrys_status(self):
        registry = {
            "m1": ModelRegistryEntry(
                model_id="m1", status=ModelStatus.CONDITIONAL, owner="x", risk_classification="LOW",
                approved_use_cases=[],
            )
        }
        self.assertEqual(get_status("m1", registry=registry, fail_closed=True), ModelStatus.CONDITIONAL)


if __name__ == "__main__":
    unittest.main()
