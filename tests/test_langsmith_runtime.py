import os

from pydantic import SecretStr

from src.config import Settings
from src.observability.langsmith import configure_langsmith


def test_configure_langsmith_clears_cached_environment(monkeypatch):
    from langsmith import utils as langsmith_utils

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_WORKSPACE_ID", raising=False)
    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()
    assert langsmith_utils.get_env_var("TRACING", default="") == ""

    try:
        configure_langsmith(Settings(
            _env_file=None,
            app_env="test",
            langsmith_tracing=True,
            langsmith_api_key=SecretStr("lsv2_sk_test"),
            langsmith_project="smartreco-test",
            langsmith_workspace_id="00000000-0000-0000-0000-000000000001",
        ))

        assert langsmith_utils.get_env_var("TRACING", default="") == "true"
        assert langsmith_utils.get_tracer_project() == "smartreco-test"
        assert langsmith_utils.get_env_var("WORKSPACE_ID") == "00000000-0000-0000-0000-000000000001"
    finally:
        os.environ["LANGSMITH_TRACING"] = "false"
        langsmith_utils.get_env_var.cache_clear()
        langsmith_utils.get_tracer_project.cache_clear()
