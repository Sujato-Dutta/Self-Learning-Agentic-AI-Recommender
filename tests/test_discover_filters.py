import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.main import app
from src.models import Product

COURSE_CARD_IDS = re.compile(
    r'<article class="course-card[^"]*"[^>]*data-product-id="([^"]+)"'
)


def _login(client: TestClient) -> None:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    response = client.post(
        "/auth/login",
        data={
            "email": "learner@smartreco.dev",
            "password": "LearnerDemo123!",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200


def _expected_product_ids(
    db, *, q: str = "", category: str = "", difficulty: str = ""
) -> set[str]:
    products = list(db.scalars(select(Product).where(Product.is_active.is_(True))))
    if q:
        needle = q.lower()
        products = [
            product
            for product in products
            if needle
            in (
                f"{product.title} {product.description} {product.skill_bundle} "
                f"{' '.join(product.skills)}"
            ).lower()
        ]
    if category:
        products = [product for product in products if product.category == category]
    if difficulty:
        products = [product for product in products if product.difficulty == difficulty]
    return {product.id for product in products}


@pytest.mark.parametrize(
    "params",
    [
        {"q": "Python"},
        {"category": "Data Science"},
        {"difficulty": "advanced"},
        {"q": "Python", "difficulty": "intermediate"},
    ],
)
def test_active_filters_render_only_matching_catalog_cards(db, params):
    with TestClient(app) as client:
        _login(client)
        response = client.get("/discover", params=params)

    assert response.status_code == 200
    expected_ids = _expected_product_ids(db, **params)
    assert set(COURSE_CARD_IDS.findall(response.text)) == expected_ids
    assert f"<span>{len(expected_ids)} results</span>" in response.text
    assert 'id="recommended-for-you"' not in response.text
    assert "data-nba-card" not in response.text

    expected_bundle = db.scalar(
        select(Product.id).where(Product.id.in_(expected_ids), Product.is_bundle.is_(True)).limit(1)
    )
    assert ('id="exclusive-course-bundles"' in response.text) is bool(expected_bundle)


def test_filtered_bundle_section_only_appears_for_matching_bundles(db):
    with TestClient(app) as client:
        _login(client)
        without_bundles = client.get("/discover", params={"category": "Data Science"})
        with_bundles = client.get("/discover", params={"q": "Professional"})

    assert 'id="exclusive-course-bundles"' not in without_bundles.text
    assert 'id="exclusive-course-bundles"' in with_bundles.text
    assert set(COURSE_CARD_IDS.findall(with_bundles.text)) == _expected_product_ids(
        db, q="Professional"
    )


def test_filter_form_preserves_normalized_search_and_ignores_blank_search():
    with TestClient(app) as client:
        _login(client)
        combined = client.get(
            "/discover", params={"q": "  Python  ", "category": "Data Science"}
        )
        blank = client.get("/discover", params={"q": "   "})

    assert '<input type="hidden" name="q" value="Python">' in combined.text
    assert 'id="recommended-for-you"' not in combined.text
    assert 'id="recommended-for-you"' in blank.text
    assert 'id="exclusive-course-bundles"' in blank.text
    assert '<input type="hidden" name="q"' not in blank.text
