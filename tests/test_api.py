from fastapi.testclient import TestClient

from src.main import app


def test_public_health_and_landing():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        readiness = client.get("/health/ready")
        assert readiness.status_code == 200
        assert readiness.json()["checks"]["schema"] == "ok"
        landing = client.get("/")
        assert landing.status_code == 200
        assert "Recommend the right course" in landing.text
        assert 'href="/courses/agentic-ai-professional"' in landing.text
        assert 'data-cart-label="Add track to cart"' in landing.text
        assert 'data-product-id="' in landing.text
        assert "Mesh intelligence active" in landing.text
        assert "Mesh spend gate paused" not in landing.text
        assert "Deterministic copy active" not in landing.text
        assert client.get("/assets/company%20logos/scaler_logo.png").status_code == 200
        assert client.get("/assets/company%20logos/unacademy_logo_clean.png").status_code == 200
        assert client.get("/assets/course%20covers/advanced_agentic_ai.png").status_code == 200


def test_auth_and_role_enforcement():
    with TestClient(app) as client:
        client.get("/login")
        csrf = client.cookies.get("csrf_token")
        response = client.post("/auth/login", data={"email": "learner@smartreco.dev",
            "password": "LearnerDemo123!", "csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/discover").status_code == 200
        assert client.get("/admin").status_code == 403


def test_batch_event_endpoint_and_duplicate_handling():
    with TestClient(app) as client:
        client.get("/login")
        csrf = client.cookies.get("csrf_token")
        client.post("/auth/login", data={"email": "learner@smartreco.dev", "password": "LearnerDemo123!", "csrf_token": csrf})
        event = {"event_id": "api-event-unique", "session_id": "api-session-unique", "event_type": "search",
                 "search_query": "transformers", "metadata": {}, "occurred_at": "2026-08-06T10:00:00Z"}
        assert client.post("/api/events/batch", json={"events": [event]}).json()["accepted"] == 1
        assert client.post("/api/events/batch", json={"events": [event]}).json()["duplicates"] == 1


def test_admin_dashboard_and_course_detail_render():
    with TestClient(app) as client:
        client.get("/login")
        csrf = client.cookies.get("csrf_token")
        client.post("/auth/login", data={"email": "admin@smartreco.dev", "password": "AdminDemo123!",
                    "csrf_token": csrf})
        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "Recommendation intelligence" in dashboard.text
        assert "Next-best actions" in dashboard.text
        assert "Expected revenue" in dashboard.text
        client.post("/auth/logout", data={"csrf_token": csrf})
        client.get("/login")
        csrf = client.cookies.get("csrf_token")
        client.post("/auth/login", data={"email": "learner@smartreco.dev", "password": "LearnerDemo123!",
                    "csrf_token": csrf})
        course = client.get("/courses/agentic-ai-foundations")
        assert course.status_code == 200
        assert "Agentic AI Foundations" in course.text
        learning = client.get("/learning", follow_redirects=False)
        assert learning.status_code == 303 and learning.headers["location"] == "/my-courses"
        for path, heading in [
            ("/my-courses", "My Courses"),
            ("/saved", "Saved for later"),
            ("/cart", "Your cart"),
        ]:
            page = client.get(path)
            assert page.status_code == 200
            assert heading in page.text
