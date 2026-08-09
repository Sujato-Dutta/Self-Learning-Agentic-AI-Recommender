from decimal import Decimal

import pytest

from src.models import Product
from src.services.bundle_pricing_service import (
    ZERO_MONEY,
    calculate_personalized_offer,
    is_product_offer_eligible,
)


def _course(product_id: str, price: str, *, active: bool = True) -> Product:
    return Product(
        id=product_id,
        title=f"Course {product_id}",
        slug=f"course-{product_id}",
        description="A sufficiently detailed course description.",
        category="AI",
        difficulty="intermediate",
        price=Decimal(price),
        original_price=None,
        duration_hours=10,
        skill_bundle="Agentic AI",
        is_bundle=False,
        bundled_product_ids=[],
        is_active=active,
    )


def _bundle(
    component_ids: list[str],
    *,
    price: str = "480.00",
    product_id: str = "bundle",
    active: bool = True,
) -> Product:
    return Product(
        id=product_id,
        title="Professional Bundle",
        slug=f"professional-{product_id}",
        description="A sufficiently detailed professional bundle description.",
        category="Professional Bundle",
        difficulty="all-levels",
        price=Decimal(price),
        original_price=Decimal("600.00"),
        duration_hours=30,
        skill_bundle="Agentic AI",
        is_bundle=True,
        bundled_product_ids=component_ids,
        is_active=active,
    )


def test_partial_bundle_ownership_keeps_offer_and_credits_discounted_share():
    courses = [_course("a", "100.00"), _course("b", "200.00"), _course("c", "300.00")]
    bundle = _bundle([course.id for course in courses])

    offer = calculate_personalized_offer(bundle, courses, {"a"})

    assert offer.eligible is True
    assert offer.eligibility_reason is None
    assert [item.product_id for item in offer.owned_components] == ["a"]
    assert [item.product_id for item in offer.remaining_components] == ["b", "c"]
    assert offer.full_standalone_subtotal == Decimal("600.00")
    assert offer.owned_standalone_subtotal == Decimal("100.00")
    assert offer.remaining_standalone_subtotal == Decimal("500.00")
    assert offer.discount_rate == Decimal("0.200000")
    assert offer.discount_percent == 20
    assert offer.ownership_credit == Decimal("80.00")
    assert offer.personalized_price == Decimal("400.00")
    assert offer.savings == Decimal("100.00")
    assert offer.has_ownership_credit is True
    assert offer.catalog_price - offer.ownership_credit == offer.personalized_price


def test_multiple_owned_components_receive_one_reconciled_credit():
    courses = [_course("a", "100.00"), _course("b", "200.00"), _course("c", "300.00")]
    bundle = _bundle([course.id for course in courses])

    offer = calculate_personalized_offer(bundle, reversed(courses), {"a", "b"})

    assert [item.product_id for item in offer.owned_components] == ["a", "b"]
    assert [item.product_id for item in offer.remaining_components] == ["c"]
    assert offer.owned_standalone_subtotal == Decimal("300.00")
    assert offer.remaining_standalone_subtotal == Decimal("300.00")
    assert offer.ownership_credit == Decimal("240.00")
    assert offer.personalized_price == Decimal("240.00")
    assert offer.savings == Decimal("60.00")


def test_unowned_bundle_retains_catalog_price_and_original_discount():
    courses = [_course("a", "100.00"), _course("b", "200.00"), _course("c", "300.00")]
    bundle = _bundle([course.id for course in courses])

    offer = calculate_personalized_offer(bundle, courses, set())

    assert offer.eligible is True
    assert offer.owned_components == ()
    assert offer.ownership_credit == ZERO_MONEY
    assert offer.personalized_price == Decimal("480.00")
    assert offer.savings == Decimal("120.00")


def test_all_components_owned_or_exact_bundle_owned_makes_bundle_ineligible():
    courses = [_course("a", "100.00"), _course("b", "200.00"), _course("c", "300.00")]
    bundle = _bundle([course.id for course in courses])

    all_components_owned = calculate_personalized_offer(bundle, courses, {"a", "b", "c"})
    exact_bundle_owned = calculate_personalized_offer(bundle, courses, {bundle.id})

    assert all_components_owned.eligible is False
    assert all_components_owned.eligibility_reason == "all_components_already_owned"
    assert all_components_owned.personalized_price == ZERO_MONEY
    assert all_components_owned.remaining_components == ()
    assert is_product_offer_eligible(bundle, {"a", "b", "c"}) is False

    assert exact_bundle_owned.eligible is False
    assert exact_bundle_owned.eligibility_reason == "product_already_owned"
    assert exact_bundle_owned.personalized_price == ZERO_MONEY
    assert exact_bundle_owned.remaining_components == ()
    assert is_product_offer_eligible(bundle, {bundle.id}) is False


def test_exact_owned_course_is_ineligible_but_an_unowned_course_is_eligible():
    course = _course("course", "2499.00")

    owned_offer = calculate_personalized_offer(course, (), {course.id})
    unowned_offer = calculate_personalized_offer(course, (), set())

    assert owned_offer.eligible is False
    assert owned_offer.eligibility_reason == "product_already_owned"
    assert owned_offer.ownership_credit == Decimal("2499.00")
    assert owned_offer.personalized_price == ZERO_MONEY
    assert is_product_offer_eligible(course, {course.id}) is False

    assert unowned_offer.eligible is True
    assert unowned_offer.personalized_price == Decimal("2499.00")
    assert is_product_offer_eligible(course, set()) is True


def test_half_cent_residual_price_rounds_up_and_never_creates_a_free_course():
    courses = [_course("a", "0.01"), _course("b", "0.01")]
    bundle = _bundle([course.id for course in courses], price="0.01")

    offer = calculate_personalized_offer(bundle, courses, {"a"})

    assert offer.eligible is True
    assert offer.ownership_credit == ZERO_MONEY
    assert offer.personalized_price == Decimal("0.01")
    assert offer.savings == ZERO_MONEY
    assert offer.catalog_price - offer.ownership_credit == offer.personalized_price
    monetary_values = (
        offer.catalog_price,
        offer.full_standalone_subtotal,
        offer.owned_standalone_subtotal,
        offer.remaining_standalone_subtotal,
        offer.ownership_credit,
        offer.personalized_price,
        offer.savings,
    )
    assert all(value >= ZERO_MONEY and value.as_tuple().exponent == -2 for value in monetary_values)


def test_incomplete_bundle_component_data_fails_closed():
    courses = [_course("a", "100.00"), _course("b", "200.00")]
    bundle = _bundle(["a", "b", "missing"])

    with pytest.raises(ValueError, match="unresolved components: missing"):
        calculate_personalized_offer(bundle, courses, {"a"})
