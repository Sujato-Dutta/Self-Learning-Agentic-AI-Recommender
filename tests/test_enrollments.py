from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.database import SessionLocal, apply_sqlite_catalog_migration
from src.main import app
from src.models import Event, Product, User, UserEnrollment
from src.services.behavior_service import aggregate_profile

LEARNER_EMAIL = "learner@smartreco.dev"
LEARNER_PASSWORD = "LearnerDemo123!"


def login(client: TestClient, email: str = LEARNER_EMAIL, password: str = LEARNER_PASSWORD) -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def register(client: TestClient, email: str, password: str) -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/register",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def purchase(
    client: TestClient,
    csrf: str,
    product_ids: list[str],
    source: str = "cart_checkout",
):
    return client.post(
        "/api/demo-purchases",
        headers={"X-CSRF-Token": csrf},
        json={"product_ids": product_ids, "source": source},
    )


def product_by_slug(slug: str) -> Product:
    with SessionLocal() as db:
        product = db.scalar(select(Product).where(Product.slug == slug))
        assert product
        db.expunge(product)
        return product


def user_by_email(email: str = LEARNER_EMAIL) -> User:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user
        db.expunge(user)
        return user


def product_card(page: str, product_id: str) -> str:
    marker = f'data-product-id="{product_id}"'
    position = page.index(marker)
    start = page.rfind("<article", 0, position)
    end = page.find("</article>", position)
    assert start >= 0 and end >= 0
    return page[start:end + len("</article>")]


def test_authenticated_direct_purchase_is_durable_and_records_a_server_event():
    product = product_by_slug("python-foundations")
    user = user_by_email()

    with TestClient(app) as client:
        csrf = login(client)
        response = purchase(
            client,
            csrf,
            [product.id],
            source="course_detail_demo_purchase",
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["purchased_product_ids"] == [product.id]
    assert payload["enrolled_product_ids"] == [product.id]
    assert payload["newly_enrolled_product_ids"] == [product.id]
    assert payload["already_enrolled_product_ids"] == []
    assert payload["event_ids"] == [f"enrollment:{user.id}:{product.id}"]
    assert payload["profile_version"] is not None

    with SessionLocal() as db:
        enrollment = db.scalar(select(UserEnrollment).where(
            UserEnrollment.user_id == user.id,
            UserEnrollment.product_id == product.id,
        ))
        event = db.scalar(select(Event).where(Event.event_id == payload["event_ids"][0]))
        assert enrollment and enrollment.source_product_id == product.id
        assert event and event.user_id == user.id
        assert event.event_type == "enrollment"
        assert event.event_metadata["source"] == "course_detail_demo_purchase"
        assert event.event_metadata["pricing"]["payable"] == str(product.price)


def test_purchase_requires_authentication_and_matching_csrf_without_mutation():
    product = product_by_slug("python-foundations")
    user = user_by_email()

    with TestClient(app) as anonymous:
        anonymous.get("/login")
        csrf = anonymous.cookies.get("csrf_token")
        response = purchase(anonymous, csrf, [product.id])
        assert response.status_code == 401

    with TestClient(app) as authenticated:
        login(authenticated)
        response = authenticated.post(
            "/api/demo-purchases",
            json={"product_ids": [product.id], "source": "cart_checkout"},
        )
        assert response.status_code == 403

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(UserEnrollment).where(
            UserEnrollment.user_id == user.id,
            UserEnrollment.product_id == product.id,
        )) == 0
        assert db.scalar(select(func.count()).select_from(Event).where(
            Event.user_id == user.id,
            Event.product_id == product.id,
            Event.event_type == "enrollment",
        )) == 0


def test_invalid_mixed_purchase_is_atomic():
    product = product_by_slug("python-foundations")
    user = user_by_email()
    unavailable_id = "00000000-0000-0000-0000-000000000000"

    with TestClient(app) as client:
        csrf = login(client)
        response = purchase(client, csrf, [product.id, unavailable_id])

    assert response.status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(UserEnrollment).where(
            UserEnrollment.user_id == user.id,
        )) == 0
        assert db.scalar(select(func.count()).select_from(Event).where(
            Event.user_id == user.id,
            Event.event_type == "enrollment",
        )) == 0


def test_direct_purchase_retry_is_idempotent():
    product = product_by_slug("power-bi-analytics")
    user = user_by_email()

    with TestClient(app) as client:
        csrf = login(client)
        first = purchase(client, csrf, [product.id])
        second = purchase(client, csrf, [product.id])

    assert first.status_code == second.status_code == 200
    assert first.json()["newly_enrolled_product_ids"] == [product.id]
    assert len(first.json()["event_ids"]) == 1
    assert second.json()["newly_enrolled_product_ids"] == []
    assert second.json()["already_enrolled_product_ids"] == [product.id]
    assert second.json()["event_ids"] == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(UserEnrollment).where(
            UserEnrollment.user_id == user.id,
            UserEnrollment.product_id == product.id,
        )) == 1
        assert db.scalar(select(func.count()).select_from(Event).where(
            Event.user_id == user.id,
            Event.product_id == product.id,
            Event.event_type == "enrollment",
        )) == 1


def test_bundle_purchase_expands_components_and_normalizes_component_overlap():
    bundle = product_by_slug("agentic-ai-professional")
    user = user_by_email()
    component_id = bundle.bundled_product_ids[0]
    expected_ids = {bundle.id, *bundle.bundled_product_ids}

    with TestClient(app) as client:
        csrf = login(client)
        response = purchase(client, csrf, [component_id, bundle.id])

    assert response.status_code == 200
    payload = response.json()
    assert payload["purchased_product_ids"] == [bundle.id]
    assert set(payload["enrolled_product_ids"]) == expected_ids
    assert set(payload["newly_enrolled_product_ids"]) == expected_ids
    assert payload["already_enrolled_product_ids"] == []
    assert payload["event_ids"] == [f"enrollment:{user.id}:{bundle.id}"]

    with SessionLocal() as db:
        enrollments = list(db.scalars(select(UserEnrollment).where(
            UserEnrollment.user_id == user.id,
            UserEnrollment.product_id.in_(expected_ids),
        )))
        assert {enrollment.product_id for enrollment in enrollments} == expected_ids
        assert all(enrollment.source_product_id == bundle.id for enrollment in enrollments)
        events = list(db.scalars(select(Event).where(
            Event.user_id == user.id,
            Event.event_type == "enrollment",
        )))
        assert [event.product_id for event in events] == [bundle.id]


def test_enrollments_are_isolated_per_user():
    first_product = product_by_slug("python-foundations")
    second_product = product_by_slug("power-bi-analytics")
    second_email = "second-learner@smartreco.dev"
    second_password = "SecondLearner123!"

    with TestClient(app) as first_client:
        first_csrf = login(first_client)
        assert purchase(first_client, first_csrf, [first_product.id]).status_code == 200
        first_page = first_client.get("/my-courses")

    with TestClient(app) as second_client:
        second_csrf = register(second_client, second_email, second_password)
        assert purchase(second_client, second_csrf, [second_product.id]).status_code == 200
        second_page = second_client.get("/my-courses")

    assert first_product.title in first_page.text
    assert second_product.title not in first_page.text
    assert second_product.title in second_page.text
    assert first_product.title not in second_page.text
    with SessionLocal() as db:
        first_user = db.scalar(select(User).where(User.email == LEARNER_EMAIL))
        second_user = db.scalar(select(User).where(User.email == second_email))
        assert first_user and second_user
        assert set(db.scalars(select(UserEnrollment.product_id).where(
            UserEnrollment.user_id == first_user.id
        ))) == {first_product.id}
        assert set(db.scalars(select(UserEnrollment.product_id).where(
            UserEnrollment.user_id == second_user.id
        ))) == {second_product.id}


def test_my_courses_shows_unique_bundle_leaf_courses_and_inert_learning_buttons():
    bundle = product_by_slug("agentic-ai-professional")
    components = [product_by_slug(slug) for slug in (
        "agentic-ai-foundations",
        "advanced-agentic-ai",
        "production-rag-systems",
    )]

    with TestClient(app) as client:
        csrf = login(client)
        assert purchase(client, csrf, [bundle.id]).status_code == 200
        page = client.get("/my-courses")

    assert page.status_code == 200
    assert "My Courses" in page.text
    for component in components:
        assert component.id in bundle.bundled_product_ids
        assert page.text.count(f'data-product-id="{component.id}"') == 1
        assert component.title in page.text
    assert f'data-product-id="{bundle.id}"' not in page.text
    button = '<button class="start-learning-button" type="button">Start Learning</button>'
    assert page.text.count(button) == len(components)


def test_discover_and_detail_replace_purchase_actions_with_owned_state():
    product = product_by_slug("python-foundations")

    with TestClient(app) as client:
        csrf = login(client)
        assert purchase(client, csrf, [product.id]).status_code == 200
        discover = client.get("/discover", params={"category": product.category})
        detail = client.get(f"/courses/{product.slug}")

    assert discover.status_code == detail.status_code == 200
    card = product_card(discover.text, product.id)
    assert "Already purchased" in card
    assert "data-cart" not in card
    assert "data-buy" not in card
    assert "Buy now" not in card
    assert "Already purchased" in detail.text
    assert "data-demo-purchase" not in detail.text
    assert "Buy now (demo)" not in detail.text
    assert "Buy course (demo)" not in detail.text


def test_purchase_is_a_recommendation_signal_but_not_course_completion():
    product = product_by_slug("time-series-forecasting")
    user = user_by_email()
    with SessionLocal() as db:
        before = aggregate_profile(db, user.id, persist=False)
    before_score = before["skill_bundle_scores"].get(product.skill_bundle, 0)

    with TestClient(app) as client:
        csrf = login(client)
        response = purchase(client, csrf, [product.id])
        assert response.status_code == 200

    with SessionLocal() as db:
        profile = aggregate_profile(db, user.id, persist=False)

    assert product.id in profile["purchased_product_ids"]
    assert product.id in profile["excluded_product_ids"]
    assert product.id not in profile["completed_product_ids"]
    assert profile["skill_bundle_scores"][product.skill_bundle] > before_score
    assert f"enrollment: {product.title}" in profile["high_intent_signals"]


def test_erasing_behavior_preserves_enrollment_and_my_courses_access():
    product = product_by_slug("python-foundations")
    user = user_by_email()

    with TestClient(app) as client:
        csrf = login(client)
        assert purchase(client, csrf, [product.id]).status_code == 200
        erased = client.delete("/api/behavior")
        page = client.get("/my-courses")

    assert erased.status_code == 200
    assert page.status_code == 200
    assert product.title in page.text
    assert "Start Learning" in page.text
    with SessionLocal() as db:
        assert db.scalar(select(UserEnrollment.id).where(
            UserEnrollment.user_id == user.id,
            UserEnrollment.product_id == product.id,
        )) is not None
        assert db.scalar(select(Event.id).where(
            Event.user_id == user.id,
            Event.event_type == "enrollment",
            Event.product_id == product.id,
        )) is None
        profile = aggregate_profile(db, user.id, persist=False)
        assert product.id in profile["purchased_product_ids"]
        assert product.id in profile["excluded_product_ids"]
        assert product.id not in profile["completed_product_ids"]


def test_local_upgrade_backfills_legacy_bundle_access_idempotently():
    bundle = product_by_slug("agentic-ai-professional")
    user = user_by_email()
    with SessionLocal() as db:
        db.add(Event(
            event_id="legacy-bundle-enrollment",
            user_id=user.id,
            session_id="legacy-purchase-session",
            event_type="enrollment",
            product_id=bundle.id,
            event_metadata={"source": "legacy_checkout"},
            occurred_at=bundle.created_at,
        ))
        db.commit()

    apply_sqlite_catalog_migration()
    apply_sqlite_catalog_migration()

    with SessionLocal() as db:
        entitlement_ids = set(db.scalars(select(UserEnrollment.product_id).where(
            UserEnrollment.user_id == user.id,
        )))
    assert entitlement_ids == {bundle.id, *bundle.bundled_product_ids}
