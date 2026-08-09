from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.database import SessionLocal
from src.main import app
from src.models import Event, Product, User

LEARNER_EMAIL = "learner@smartreco.dev"
LEARNER_PASSWORD = "LearnerDemo123!"


def _login(client: TestClient) -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/login",
        data={"email": LEARNER_EMAIL, "password": LEARNER_PASSWORD, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def _register(client: TestClient, email: str) -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/register",
        data={"email": email, "password": "QuoteTestLearner123!", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def _purchase(client: TestClient, csrf: str, product_ids: list[str]):
    return client.post(
        "/api/demo-purchases",
        headers={"X-CSRF-Token": csrf},
        json={"product_ids": product_ids, "source": "cart_checkout"},
    )


def _product(slug: str) -> Product:
    with SessionLocal() as db:
        product = db.scalar(select(Product).where(Product.slug == slug))
        assert product
        db.expunge(product)
        return product


@pytest.mark.parametrize("owned_count", [1, 2])
def test_partial_bundle_checkout_quotes_live_owned_component_credit(owned_count: int):
    bundle = _product("agentic-ai-professional")
    with SessionLocal() as db:
        components = list(db.scalars(select(Product).where(
            Product.id.in_(bundle.bundled_product_ids)
        )))
        by_id = {product.id: product for product in components}
        ordered_components = [by_id[product_id] for product_id in bundle.bundled_product_ids]
    owned_components = ordered_components[:owned_count]
    owned_ids = {product.id for product in owned_components}

    with TestClient(app) as client:
        csrf = _login(client)
        owned_purchase = _purchase(client, csrf, [product.id for product in owned_components])
        assert owned_purchase.status_code == 200
        response = _purchase(client, csrf, [bundle.id])

    assert response.status_code == 200
    payload = response.json()
    offer = payload["offers"][0]
    full_subtotal = sum((product.price for product in ordered_components), start=Decimal(0))
    owned_subtotal = sum((product.price for product in owned_components), start=Decimal(0))
    remaining_subtotal = full_subtotal - owned_subtotal
    expected_payable = (
        remaining_subtotal * bundle.price / full_subtotal
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    expected_credit = bundle.price - expected_payable

    assert offer["product_id"] == bundle.id
    assert offer["eligible"] is True
    assert set(offer["owned_component_ids"]) == owned_ids
    assert set(offer["remaining_component_ids"]) == {
        product.id for product in ordered_components if product.id not in owned_ids
    }
    assert Decimal(offer["owned_standalone_subtotal"]) == owned_subtotal
    assert Decimal(offer["remaining_standalone_subtotal"]) == remaining_subtotal
    assert Decimal(offer["ownership_credit"]) == expected_credit
    assert Decimal(offer["personalized_price"]) == expected_payable
    assert Decimal(payload["payable_total"]) == expected_payable
    assert Decimal(payload["ownership_credit_total"]) == expected_credit
    assert not owned_ids.intersection(payload["newly_enrolled_product_ids"])
    assert owned_ids.issubset(payload["already_enrolled_product_ids"])

    with SessionLocal() as db:
        user_id = db.scalar(select(User.id).where(User.email == LEARNER_EMAIL))
        event = db.scalar(select(Event).where(
            Event.event_id == f"enrollment:{user_id}:{bundle.id}"
        ))
        assert event
        pricing = event.event_metadata["pricing"]
        assert Decimal(pricing["catalog_price"]) == bundle.price
        assert Decimal(pricing["full_standalone_subtotal"]) == full_subtotal
        assert Decimal(pricing["owned_standalone_subtotal"]) == owned_subtotal
        assert Decimal(pricing["ownership_credit"]) == expected_credit
        assert Decimal(pricing["remaining_standalone_subtotal"]) == remaining_subtotal
        assert Decimal(pricing["payable"]) == expected_payable
        assert pricing["eligible"] is True
        assert pricing["eligibility_reason"] is None
        assert pricing["discount_percent"] > 0
        assert set(pricing["owned_component_ids"]) == owned_ids


def test_exact_owned_retry_is_idempotent_and_returns_zero_server_quote():
    course = _product("python-foundations")

    with TestClient(app) as client:
        csrf = _login(client)
        first = _purchase(client, csrf, [course.id])
        retry = _purchase(client, csrf, [course.id])

    assert first.status_code == retry.status_code == 200
    payload = retry.json()
    offer = payload["offers"][0]
    assert offer["eligible"] is False
    assert offer["eligibility_reason"] == "product_already_owned"
    assert Decimal(offer["personalized_price"]) == Decimal("0.00")
    assert Decimal(offer["ownership_credit"]) == course.price
    assert Decimal(payload["payable_total"]) == Decimal("0.00")
    assert Decimal(payload["ownership_credit_total"]) == course.price
    assert payload["newly_enrolled_product_ids"] == []
    assert payload["already_enrolled_product_ids"] == [course.id]
    assert payload["event_ids"] == []


def test_behavior_budget_uses_paid_bundle_completion_price_not_catalog_price():
    bundle = _product("agentic-ai-professional")
    owned_component = _product("agentic-ai-foundations")

    with TestClient(app) as client:
        csrf = _register(client, "payable-budget@smartreco.dev")
        first = _purchase(client, csrf, [owned_component.id])
        completion = _purchase(client, csrf, [bundle.id])
        profile_response = client.get("/api/journey-twin")

    assert first.status_code == completion.status_code == profile_response.status_code == 200
    payable = Decimal(completion.json()["payable_total"])
    assert payable < bundle.price
    expected_observed_max = max(owned_component.price, payable)
    assert Decimal(str(profile_response.json()["budget_max"])) == (
        expected_observed_max * Decimal("1.15")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def test_overlapping_bundle_quote_is_independent_of_client_product_order():
    agentic = _product("agentic-ai-professional")
    generative = _product("generative-ai-engineer-professional")

    with TestClient(app) as first_client:
        first_csrf = _login(first_client)
        first = _purchase(first_client, first_csrf, [agentic.id, generative.id])

    with TestClient(app) as second_client:
        second_csrf = _register(second_client, "quote-order@smartreco.dev")
        second = _purchase(second_client, second_csrf, [generative.id, agentic.id])

    assert first.status_code == second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["payable_total"] == second_payload["payable_total"]
    assert first_payload["ownership_credit_total"] == second_payload["ownership_credit_total"]
    first_offers = {offer["product_id"]: offer for offer in first_payload["offers"]}
    second_offers = {offer["product_id"]: offer for offer in second_payload["offers"]}
    assert first_offers == second_offers
    assert Decimal(first_payload["payable_total"]) == generative.price
    assert first_offers[generative.id]["eligible"] is True
    assert first_offers[agentic.id]["eligible"] is False
    assert first_offers[agentic.id]["eligibility_reason"] == "all_components_already_owned"
