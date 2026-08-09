from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.main import app, format_inr
from src.models import Product, User, UserEnrollment
from src.services.bundle_pricing_service import calculate_personalized_offer


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
        follow_redirects=False,
    )
    assert response.status_code == 303


def _card(html: str, product_id: str) -> str:
    return html.split(f'data-product-id="{product_id}"', 1)[1].split("</article>", 1)[0]


def test_partial_bundle_offer_is_consistent_across_learner_surfaces(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    bundle = db.scalar(select(Product).where(Product.slug == "agentic-ai-professional"))
    owned_course = db.scalar(select(Product).where(Product.slug == "agentic-ai-foundations"))
    assert user and bundle and owned_course
    components = list(db.scalars(select(Product).where(
        Product.id.in_(bundle.bundled_product_ids)
    )))
    db.add(UserEnrollment(
        user_id=user.id,
        product_id=owned_course.id,
        source_product_id=owned_course.id,
    ))
    db.commit()
    offer = calculate_personalized_offer(bundle, components, {owned_course.id})

    with TestClient(app) as client:
        _login(client)
        discover = client.get("/discover", params={"category": "Professional Bundle"})
        detail = client.get(f"/courses/{bundle.slug}")
        cart = client.get("/cart")
        landing = client.get("/")

    expected_price = format_inr(offer.personalized_price)
    expected_credit = format_inr(offer.ownership_credit)
    for response in (discover, detail, cart, landing):
        assert response.status_code == 200
        assert bundle.title in response.text
        assert "COMPLETE YOUR BUNDLE" in response.text
        assert expected_price in response.text
        assert expected_credit in response.text

    discover_card = _card(discover.text, bundle.id)
    cart_card = _card(cart.text, bundle.id)
    for card in (discover_card, cart_card):
        assert f'data-price="{offer.personalized_price}"' in card
        assert f'data-ownership-credit="{offer.ownership_credit}"' in card
        assert "1 course already yours" in card
        assert "2 courses remaining" in card
        assert "Complete bundle" in card

    assert "Already yours" in detail.text
    assert "Included on completion" in detail.text
    assert "You pay only for the 2 courses still missing." in detail.text
    assert f'data-product-id="{bundle.id}"' in landing.text
    assert 'data-cart-label="Complete bundle"' in landing.text


def test_cart_uses_server_rendered_offer_amounts_without_sending_client_prices():
    javascript = Path("frontend/static/js/app.js").read_text(encoding="utf-8")

    assert 'body: JSON.stringify({ product_ids: normalizedIds, source })' in javascript
    assert "result?.payable_total" in javascript
    assert "result?.ownership_credit_total" in javascript
    assert "Number(card.dataset.price)" in javascript
    assert "Number(card.dataset.ownershipCredit || 0)" in javascript
    assert "bundleComponents.forEach" in javascript
    assert "componentIds.forEach" in javascript
