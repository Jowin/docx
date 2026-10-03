"""The contract layer holds the line that PRD 1 draws (CTR-12, CTR-13, CTR-16)."""

from __future__ import annotations

import pytest
from dataextractor_designtime.records import ValidationError

from dataextractor_designtime.contracts import (
    ExtractedValue,
    FieldDef,
    FieldSchema,
    ReviewQueueEntry,
    canonical_json,
    sha256_of,
    validate_reason_code,
)


def test_extracted_value_requires_a_locatable_source():
    ExtractedValue(value=12400.0, source="body:segment_0", confidence=0.9)
    ExtractedValue(value=1, source="attachment:inv.xlsx!Summary!B14", confidence=0.9)
    ExtractedValue(value=1, source="embedded:1:thread/0", confidence=0.9)
    with pytest.raises(ValidationError):
        ExtractedValue(value=1, source="somewhere in the email", confidence=0.9)


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        ExtractedValue(value=1, source="body:segment_0", confidence=1.4)


def test_review_reasons_come_from_the_closed_vocabulary():
    entry = ReviewQueueEntry(
        audit_id="a",
        client_id="acme",
        workflow_id="ap",
        package_version="1.0.0",
        review_reason=["low_confidence", "missing_field:due_date"],
    )
    assert entry.review_reason == ["low_confidence", "missing_field:due_date"]
    with pytest.raises(ValidationError):
        ReviewQueueEntry(
            audit_id="a",
            client_id="acme",
            workflow_id="ap",
            package_version="1.0.0",
            review_reason=["looked_wrong"],
        )


def test_parameterised_reason_needs_an_argument():
    with pytest.raises(ValueError):
        validate_reason_code("missing_field")


def test_unsupported_schema_version_is_refused_not_coerced():
    with pytest.raises(ValidationError):
        FieldSchema(schema_version="9.9", email_type="invoice", generated_by="x")


def test_checksums_ignore_key_order_and_whitespace():
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert sha256_of(a) == sha256_of(b)
    assert canonical_json(a) == b'{"a":[1,2],"b":1}'


def test_unreviewed_artifact_is_flagged():
    schema = FieldSchema(
        email_type="invoice",
        required_fields=[FieldDef(name="amount", type="decimal", critical=True)],
        generated_by="design-agent:field-schema@0.1.0",
    )
    assert schema.unreviewed is True
    assert schema.critical_field_names() == ["amount"]
