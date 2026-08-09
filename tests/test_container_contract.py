from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_image_is_non_root_and_no_spend_by_default():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.11-slim-bookworm AS builder" in dockerfile
    assert "APP_ENV=production" in dockerfile
    assert "MESH_CALLS_ENABLED=false" in dockerfile
    assert "SCHEDULER_ENABLED=false" in dockerfile
    assert "USER smartreco" in dockerfile
    assert "exec uvicorn" in dockerfile
    assert "/health/live" in dockerfile
    assert "COPY --chown=smartreco:smartreco . ." not in dockerfile


def test_compose_uses_cloud_database_and_a_single_scheduler_owner():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "sqlite:" not in compose.lower()
    assert 'MESH_CALLS_ENABLED: ${MESH_CALLS_ENABLED:-false}' in compose
    assert 'SCHEDULER_ENABLED: "false"' in compose
    assert 'command: ["python", "-m", "src.jobs.worker"]' in compose
    assert compose.count('SCHEDULER_ENABLED: "true"') == 1
    assert 'SCHEDULER_METRICS_PORT: "9101"' in compose
    assert "/health/ready" in compose
    assert "smartreco-local" not in compose
    assert "GRAFANA_ADMIN_PASSWORD:?Set GRAFANA_ADMIN_PASSWORD" in compose

    prometheus = (ROOT / "monitoring/prometheus.yml").read_text(encoding="utf-8")
    assert 'targets: ["app:8000"]' in prometheus
    assert 'targets: ["scheduler:9101"]' in prometheus


def test_docker_context_excludes_secrets_and_local_state():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert ".env.*" in ignored
    assert "*.db" in ignored
    assert "tests" in ignored


def test_runtime_dependency_manifest_excludes_test_tooling():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    development = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "fastapi" in runtime
    assert "openai" in runtime
    assert "pytest" not in runtime
    assert "-r requirements.txt" in development
    assert "pytest" in development
    assert "ruff" in development
