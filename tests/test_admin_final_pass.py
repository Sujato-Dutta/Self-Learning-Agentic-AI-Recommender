from fastapi.testclient import TestClient
from sqlalchemy import select

from src.database import SessionLocal
from src.main import app
from src.models import AgentRun, User


def _login_admin(client: TestClient) -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/login",
        data={
            "email": "admin@smartreco.dev",
            "password": "AdminDemo123!",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def test_admin_profile_logout_and_runtime_health_are_visible():
    with TestClient(app) as client:
        csrf = _login_admin(client)
        dashboard = client.get("/admin")

        assert dashboard.status_code == 200
        assert "Administrator" in dashboard.text
        assert "admin@smartreco.dev" in dashboard.text
        assert 'action="/auth/logout"' in dashboard.text
        assert ">Log out</button>" in dashboard.text
        assert "Runtime configuration" in dashboard.text
        assert "Mesh generation" in dashboard.text
        assert "Vector retrieval" in dashboard.text
        assert "Tracing" in dashboard.text
        assert "Email" in dashboard.text
        assert "Automation" in dashboard.text
        assert "dead-lettered" not in dashboard.text
        assert "historical retry records retained" in dashboard.text

        logged_out = client.post(
            "/auth/logout",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert logged_out.status_code == 303
        assert client.get("/admin", follow_redirects=False).headers["location"] == "/login"


def test_reset_demo_clears_old_agent_runs_for_seeded_learner():
    with TestClient(app) as client:
        csrf = _login_admin(client)
        with SessionLocal() as db:
            learner = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
            assert learner
            db.add(AgentRun(
                user_id=learner.id,
                trigger_type="discover_high_intent",
                status="degraded",
                node_trace=[{"node": "validate_and_repair", "valid": True}],
            ))
            db.commit()

        response = client.post(
            "/admin/reset-demo",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303

        with SessionLocal() as db:
            learner = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
            assert learner
            assert list(db.scalars(select(AgentRun).where(AgentRun.user_id == learner.id))) == []
