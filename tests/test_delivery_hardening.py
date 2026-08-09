import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.health import readiness
from src.config import Settings
from src.database import SessionLocal
from src.jobs.scheduler import digest_preference_is_due
from src.main import app
from src.models import NotificationPreference, User


def _learner_preference(session) -> NotificationPreference:
    return session.scalar(
        select(NotificationPreference)
        .join(User, User.id == NotificationPreference.user_id)
        .where(User.email == "learner@smartreco.dev")
    )


def _login_learner(client: TestClient) -> None:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    response = client.post(
        "/auth/login",
        data={
            "email": "learner@smartreco.dev",
            "password": "LearnerDemo123!",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_unsubscribe_is_idempotent_and_does_not_disclose_token_validity():
    with SessionLocal() as session:
        preference = _learner_preference(session)
        preference.email_enabled = True
        token = preference.unsubscribe_token
        session.commit()

    with TestClient(app) as client:
        unknown = client.get(f"/unsubscribe/{'0' * 64}")
        assert unknown.status_code == 200
        assert unknown.headers["cache-control"] == "no-store"
        assert unknown.headers["referrer-policy"] == "no-referrer"

        with SessionLocal() as session:
            assert _learner_preference(session).email_enabled is True

        known = client.get(f"/unsubscribe/{token}")
        assert known.status_code == unknown.status_code
        assert known.text == unknown.text

    with SessionLocal() as session:
        assert _learner_preference(session).email_enabled is False


def test_preferences_persist_a_validated_browser_timezone():
    with TestClient(app) as client:
        _login_learner(client)
        saved = client.put(
            "/api/preferences",
            json={
                "personalization_enabled": True,
                "email_digest_enabled": True,
                "preferred_hour": 17,
                "timezone": "Asia/Kolkata",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["timezone"] == "Asia/Kolkata"

        rejected = client.put(
            "/api/preferences",
            json={
                "personalization_enabled": True,
                "email_digest_enabled": True,
                "preferred_hour": 8,
                "timezone": "Mars/Olympus_Mons",
            },
        )
        assert rejected.status_code == 422
        assert "resolvedOptions().timeZone" in client.get("/static/js/app.js").text

    with SessionLocal() as session:
        preference = _learner_preference(session)
        assert preference.timezone == "Asia/Kolkata"
        assert preference.preferred_hour == 17


def test_digest_hour_is_evaluated_in_the_learner_timezone():
    preference = NotificationPreference(
        user_id="timezone-test-user",
        email_enabled=True,
        preferred_hour=17,
        timezone="Asia/Kolkata",
    )
    assert digest_preference_is_due(
        preference,
        datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert not digest_preference_is_due(
        preference,
        datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
    )

    preference.timezone = "not/a-real-timezone"
    assert not digest_preference_is_due(
        preference,
        datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    )


def _production_settings(*, mesh_calls_enabled: bool) -> Settings:
    return Settings(
        app_env="production",
        database_url="postgresql+psycopg://user:password@db.example/smartreco",
        secret_key="production-secret-that-is-longer-than-thirty-two-characters",
        mesh_api_key="mesh-key-present",
        mesh_calls_enabled=mesh_calls_enabled,
        pinecone_api_key="pinecone-key-present",
    )


def test_production_validation_requires_mesh_calls_to_be_enabled():
    with pytest.raises(RuntimeError, match="MESH_CALLS_ENABLED=true"):
        _production_settings(mesh_calls_enabled=False).validate_production()

    _production_settings(mesh_calls_enabled=True).validate_production()


class _HealthyDatabase:
    @staticmethod
    def execute(_statement):
        return None


def _readiness_response(*, mesh_available: bool):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        mesh=SimpleNamespace(available=mesh_available, configured=True),
        vectors=SimpleNamespace(available=True),
        settings=SimpleNamespace(app_env="production"),
    )))
    response = readiness(request, _HealthyDatabase())
    return response, json.loads(response.body)


def test_production_readiness_fails_closed_when_mesh_is_disabled():
    disabled, disabled_payload = _readiness_response(mesh_available=False)
    assert disabled.status_code == 503
    assert disabled_payload["status"] == "degraded"
    assert disabled_payload["checks"]["mesh"] == "disabled"

    available, available_payload = _readiness_response(mesh_available=True)
    assert available.status_code == 200
    assert available_payload["status"] == "ready"
    assert available_payload["checks"]["mesh"] == "available"

