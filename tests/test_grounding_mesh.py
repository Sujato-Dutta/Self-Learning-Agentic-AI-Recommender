import pytest
from pydantic import SecretStr

from src.agents.validators import validate_action_copy, validate_grounding
from src.config import Settings
from src.services.mesh_client import (
    MeshClient,
    MeshUnavailable,
    parse_structured_output,
)


def test_mesh_structured_output_parses_json_fence():
    parsed = parse_structured_output('```json\n{"headline":"Valid"}\n```')
    assert parsed["headline"] == "Valid"


def test_mesh_structured_output_rejects_non_object():
    with pytest.raises(ValueError):
        parse_structured_output("[]")


def test_mesh_spending_gate_blocks_chat_and_embeddings_with_a_configured_key():
    settings = Settings(_env_file=None, app_env="test", mesh_api_key=SecretStr("rsk-test"),
                        mesh_calls_enabled=False)
    client = MeshClient(settings)
    assert client.configured and not client.available
    with pytest.raises(MeshUnavailable, match="paused"):
        client.structured_completion("system", "prompt")
    with pytest.raises(MeshUnavailable, match="paused"):
        client.embed(["course"])


def test_grounding_rejects_unknown_product_and_evidence():
    output = {"headline": "Next step", "narrative": "A sufficiently detailed grounded narrative for the learner journey.",
              "recommendations": [{"product_id": "invented", "reason": "Reason", "evidence_event_ids": ["unknown"]}]}
    valid, errors = validate_grounding(output, {"real"}, {"event-1"})
    assert not valid
    assert any("unverified" in error for error in errors)
    assert any("unsupported" in error for error in errors)


def test_grounding_accepts_verified_payload():
    output = {"headline": "Next step", "narrative": "A sufficiently detailed grounded narrative for the learner journey.",
              "recommendations": [{"product_id": "real", "reason": "Builds the right skill.", "evidence_event_ids": ["event-1"]}]}
    assert validate_grounding(output, {"real"}, {"event-1"}) == (True, [])


def test_action_copy_rejects_unverified_job_statistic():
    output = {"action_copy": {"headline": "Learn this next",
                              "message": "Take Production RAG Systems because 70% of AI jobs require RAG."}}
    valid, errors = validate_action_copy(output, "Production RAG Systems", {"2025"})
    assert not valid
    assert any("numeric claim" in error for error in errors)


def test_action_copy_accepts_selected_product_title_in_headline():
    output = {
        "action_copy": {
            "headline": "Take Production RAG Systems next",
            "message": "It closes the production deployment gap reflected in your recent learning signals.",
        }
    }

    assert validate_action_copy(output, "Production RAG Systems", set()) == (True, [])


def test_action_copy_rejects_selected_product_absent_from_headline_and_message():
    output = {
        "action_copy": {
            "headline": "Take this focused course next",
            "message": "It closes the production deployment gap reflected in your recent learning signals.",
        }
    }

    valid, errors = validate_action_copy(output, "Production RAG Systems", set())

    assert not valid
    assert "selected product is absent from action copy" in errors


def test_grounding_allows_empty_catalog_only_for_explicit_suppression_path():
    output = {"headline": "No action", "narrative": "SmartReco is waiting for a verified catalog match.",
              "recommendations": []}
    assert validate_grounding(output, set(), set(), allow_empty=True) == (True, [])
    assert validate_grounding(output, set(), set(), allow_empty=False)[0] is False
