"""Validate the local demo stack without generating AI output or sending email."""

from __future__ import annotations

import smtplib
import sys
from collections.abc import Callable

import httpx
from langsmith import Client as LangSmithClient
from pinecone import Pinecone

from src.config import Settings
from src.database import SessionLocal
from src.services.outbox_service import OutboxService


def _report(name: str, ok: bool, detail: str) -> bool:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}: {detail}")
    return ok


def _safe_check(name: str, check: Callable[[], tuple[bool, str]]) -> bool:
    try:
        ok, detail = check()
        return _report(name, ok, detail)
    except Exception as exc:  # noqa: BLE001 - preflight reports provider boundary failures safely
        return _report(name, False, f"{type(exc).__name__} (credential values were not printed)")


def main() -> int:
    settings = Settings()
    results: list[bool] = []

    results.append(_report(
        "Mesh configuration",
        bool(settings.mesh_calls_enabled and settings.mesh_api_key),
        f"enabled with {settings.mesh_model}",
    ))

    def mesh_catalog() -> tuple[bool, str]:
        if not settings.mesh_api_key or not settings.mesh_calls_enabled:
            return False, "Mesh is not enabled"
        response = httpx.get(
            f"{settings.mesh_base_url}/models",
            headers={"Authorization": f"Bearer {settings.mesh_api_key.get_secret_value()}"},
            timeout=15,
        )
        response.raise_for_status()
        catalog = response.json()
        if not isinstance(catalog, list):
            raise TypeError("Mesh model catalog has an unexpected response shape")
        model_ids = {
            model.get("id") for model in catalog
            if isinstance(model, dict) and isinstance(model.get("id"), str)
        }
        found = settings.mesh_model in model_ids
        return found, (
            f"authenticated; {settings.mesh_model} is available"
            if found else f"authenticated, but {settings.mesh_model} was not listed"
        )

    results.append(_safe_check("Mesh model catalog (no inference)", mesh_catalog))

    def pinecone_health() -> tuple[bool, str]:
        if not settings.pinecone_api_key:
            return False, "Pinecone is not configured"
        client = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
        names = {index.name for index in client.list_indexes()}
        if settings.pinecone_index_name not in names:
            return False, f"index {settings.pinecone_index_name} does not exist"
        stats = client.Index(settings.pinecone_index_name).describe_index_stats()
        namespace = (stats.namespaces or {}).get(settings.pinecone_namespace)
        vector_count = getattr(namespace, "vector_count", 0) if namespace else 0
        return True, (
            f"index {settings.pinecone_index_name}, namespace "
            f"{settings.pinecone_namespace}, {vector_count} vectors"
        )

    results.append(_safe_check("Pinecone retrieval", pinecone_health))

    def smtp_health() -> tuple[bool, str]:
        if not settings.smtp_host:
            return False, "SMTP_HOST is not configured"
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.ehlo()
            if settings.smtp_use_tls:
                smtp.starttls()
                smtp.ehlo()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(
                    settings.smtp_username,
                    settings.smtp_password.get_secret_value(),
                )
            code, _ = smtp.noop()
        return code == 250, f"authenticated with {settings.smtp_host}:{settings.smtp_port}; no email sent"

    results.append(_safe_check("SMTP transport", smtp_health))

    def langsmith_health() -> tuple[bool, str]:
        if not settings.langsmith_tracing or not settings.langsmith_api_key:
            return False, "LangSmith tracing is not enabled"
        key = settings.langsmith_api_key.get_secret_value()
        if key.startswith("lsv2_sk_") and not settings.langsmith_workspace_id:
            return False, "service key needs LANGSMITH_WORKSPACE_ID"
        client = LangSmithClient(
            api_url=settings.langsmith_endpoint,
            api_key=key,
            timeout_ms=15_000,
            workspace_id=settings.langsmith_workspace_id,
        )
        next(client.list_projects(limit=1), None)
        return True, f"authenticated; project {settings.langsmith_project}"

    results.append(_safe_check("LangSmith tracing", langsmith_health))
    results.append(_report(
        "Scheduler",
        settings.scheduler_enabled,
        "enabled for the local web process" if settings.scheduler_enabled else "disabled",
    ))

    with SessionLocal() as db:
        health = OutboxService.effective_health(db)
    catalog_ok = (
        health["active_unsynced"] == 0
        and health["pending"] == 0
        and health["processing"] == 0
        and health["failed"] == 0
    )
    results.append(_report(
        "Catalog sync",
        catalog_ok,
        (
            f"{health['active_synced']} current products synced, "
            f"{health['active_unsynced']} unsynced, {health['failed']} current failures"
        ),
    ))

    print("\nNo chat completion, embedding request, or email send was performed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
