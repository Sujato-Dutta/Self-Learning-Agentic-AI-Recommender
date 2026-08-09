import json
import time
from typing import Any

from openai import APIConnectionError, APIError, OpenAI, RateLimitError

from src.config import Settings
from src.observability.logging import logger
from src.observability.metrics import MESH_CALLS, MESH_LATENCY, MESH_TOKENS


class MeshUnavailable(RuntimeError):
    pass


class MeshClient:
    """Single controlled gateway for every model and embedding request."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        if settings.mesh_api_key and settings.mesh_calls_enabled:
            self._client = OpenAI(
                base_url=settings.mesh_base_url,
                api_key=settings.mesh_api_key.get_secret_value(),
                timeout=20.0,
                max_retries=2,
            )

    @property
    def configured(self) -> bool:
        return self.settings.mesh_api_key is not None

    @property
    def available(self) -> bool:
        return self.settings.mesh_calls_enabled and self._client is not None

    def _require_enabled(self) -> None:
        if not self.settings.mesh_calls_enabled:
            raise MeshUnavailable("Mesh API calls are paused by MESH_CALLS_ENABLED=false")
        if not self._client:
            raise MeshUnavailable("Mesh API is not configured")

    def structured_completion(self, system: str, prompt: str) -> dict[str, Any]:
        self._require_enabled()
        started = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=self.settings.mesh_model,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            )
            MESH_CALLS.labels(operation="chat", status="success").inc()
            usage = response.usage
            if usage:
                MESH_TOKENS.labels(kind="prompt").inc(usage.prompt_tokens or 0)
                MESH_TOKENS.labels(kind="completion").inc(usage.completion_tokens or 0)
            content = response.choices[0].message.content or "{}"
            return parse_structured_output(content)
        except (RateLimitError, APIConnectionError, APIError, ValueError) as exc:
            MESH_CALLS.labels(operation="chat", status="error").inc()
            logger.warning("mesh_request_failed", error_type=type(exc).__name__)
            raise MeshUnavailable("Mesh API request failed") from exc
        finally:
            MESH_LATENCY.labels(operation="chat").observe(time.perf_counter() - started)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._require_enabled()
        started = time.perf_counter()
        try:
            response = self._client.embeddings.create(model=self.settings.mesh_embedding_model, input=texts)
            MESH_CALLS.labels(operation="embedding", status="success").inc()
            if response.usage:
                MESH_TOKENS.labels(kind="embedding").inc(response.usage.total_tokens or 0)
            return [item.embedding for item in response.data]
        except (RateLimitError, APIConnectionError, APIError) as exc:
            MESH_CALLS.labels(operation="embedding", status="error").inc()
            raise MeshUnavailable("Mesh embedding request failed") from exc
        finally:
            MESH_LATENCY.labels(operation="embedding").observe(time.perf_counter() - started)


def parse_structured_output(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1])
        if content.lstrip().startswith("json"):
            content = content.lstrip()[4:].lstrip()
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("Mesh response must be a JSON object")  # noqa: TRY004 - invalid provider payload value
    return parsed
