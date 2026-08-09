import pytest
from pydantic import ValidationError

from src.config import Settings
from src.services.mesh_client import MeshClient


def test_chat_model_is_pinned_to_gpt_56_luna_through_mesh_gateway():
    settings = Settings(mesh_calls_enabled=False)

    assert settings.mesh_model == "openai/gpt-5.6-luna"
    assert settings.mesh_base_url == "https://api.meshapi.ai/v1"
    client = MeshClient(settings)
    assert client.available is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mesh_model", "openai/gpt-5.6-terra"),
        ("mesh_model", "gpt-5.6-luna"),
        ("mesh_base_url", "https://api.openai.com/v1"),
    ],
)
def test_non_luna_or_non_mesh_routing_is_rejected(field: str, value: str):
    with pytest.raises(ValidationError):
        Settings(**{field: value})
