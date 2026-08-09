"""LangSmith runtime configuration shared by the API and scheduler."""

import os

from langsmith import utils as langsmith_utils

from src.config import Settings


def configure_langsmith(settings: Settings) -> None:
    """Enable tracing after clearing SDK environment caches populated at import time."""
    if not settings.langsmith_tracing or not settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "false"
        langsmith_utils.get_env_var.cache_clear()
        langsmith_utils.get_tracer_project.cache_clear()
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    if settings.langsmith_workspace_id:
        os.environ["LANGSMITH_WORKSPACE_ID"] = settings.langsmith_workspace_id
    else:
        os.environ.pop("LANGSMITH_WORKSPACE_ID", None)

    # These helpers are cached. They may have been evaluated while importing
    # LangGraph, before FastAPI's lifespan loaded `.env` into Settings.
    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()
