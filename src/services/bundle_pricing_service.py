"""Ownership-aware pricing for course and bundle recommendation offers.

The catalog remains the source of truth for bundle membership and prices.  This
module only derives a learner-specific view: courses they already own are
credited at the bundle's existing discount rate, so they never pay twice and
the remaining courses retain the same value proposition.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from src.models import Product

MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.000001")
ZERO_MONEY = Decimal("0.00")


def _money(value: Decimal | float | str | None) -> Decimal:
    """Return a non-negative, deterministically rounded monetary value."""
    if value is None:
        return ZERO_MONEY
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return max(decimal_value, Decimal(0)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PricedBundleComponent:
    """The stable subset of a course needed to explain personalized pricing."""

    product_id: str
    title: str
    standalone_price: Decimal


@dataclass(frozen=True, slots=True)
class PersonalizedProductOffer:
    """A recommendation-safe, ownership-aware price for one catalog product.

    ``personalized_price`` is the only amount a checkout should charge for this
    offer. ``ownership_credit`` is already discounted proportionally; it is not
    the undiscounted price of owned components.
    """

    product_id: str
    is_bundle: bool
    eligible: bool
    eligibility_reason: str | None
    catalog_price: Decimal
    full_standalone_subtotal: Decimal
    owned_standalone_subtotal: Decimal
    remaining_standalone_subtotal: Decimal
    ownership_credit: Decimal
    personalized_price: Decimal
    savings: Decimal
    discount_rate: Decimal
    discount_percent: int
    owned_components: tuple[PricedBundleComponent, ...]
    remaining_components: tuple[PricedBundleComponent, ...]

    @property
    def has_ownership_credit(self) -> bool:
        return self.ownership_credit > ZERO_MONEY


def is_product_offer_eligible(
    product: Product | None,
    owned_product_ids: Iterable[str],
) -> bool:
    """Return whether a product can be offered after applying live ownership.

    An unowned bundle remains eligible while at least one component is unowned.
    Component prices are not required for this inexpensive retrieval-time guard.
    """
    if not product or not product.is_active:
        return False

    owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
    if product.id in owned:
        return False
    if not product.is_bundle:
        return True

    component_ids = set(product.bundled_product_ids or [])
    return bool(component_ids) and not component_ids.issubset(owned)


def calculate_personalized_offer(
    product: Product,
    component_products: Iterable[Product],
    owned_product_ids: Iterable[str],
) -> PersonalizedProductOffer:
    """Calculate the learner-specific amount and component breakdown.

    ``component_products`` may be empty for a standalone course. For a bundle it
    must resolve every ID in ``product.bundled_product_ids``; incomplete catalog
    data raises ``ValueError`` instead of producing a potentially incorrect
    charge. Extra products are ignored and component order follows the bundle.
    """
    owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
    catalog_price = _money(product.price)

    if not product.is_bundle:
        is_owned = product.id in owned
        eligible = bool(product.is_active and not is_owned)
        original_price = _money(product.original_price or product.price)
        savings = _money(max(original_price - catalog_price, Decimal(0))) if eligible else ZERO_MONEY
        rate = (
            ((original_price - catalog_price) / original_price)
            if eligible and original_price > ZERO_MONEY
            else Decimal(0)
        )
        return PersonalizedProductOffer(
            product_id=product.id,
            is_bundle=False,
            eligible=eligible,
            eligibility_reason=(
                "product_already_owned" if is_owned
                else "product_inactive" if not product.is_active
                else None
            ),
            catalog_price=catalog_price,
            full_standalone_subtotal=catalog_price,
            owned_standalone_subtotal=catalog_price if is_owned else ZERO_MONEY,
            remaining_standalone_subtotal=ZERO_MONEY if is_owned else catalog_price,
            ownership_credit=catalog_price if is_owned else ZERO_MONEY,
            personalized_price=catalog_price if eligible else ZERO_MONEY,
            savings=savings,
            discount_rate=rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP),
            discount_percent=int((rate * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            owned_components=(),
            remaining_components=(),
        )

    component_ids = list(dict.fromkeys(product.bundled_product_ids or []))
    if not component_ids:
        raise ValueError(f"Bundle {product.id!r} has no components")

    products_by_id = {component.id: component for component in component_products}
    missing_ids = [component_id for component_id in component_ids if component_id not in products_by_id]
    if missing_ids:
        missing = ", ".join(missing_ids)
        raise ValueError(f"Bundle {product.id!r} has unresolved components: {missing}")

    components = tuple(
        PricedBundleComponent(
            product_id=component_id,
            title=products_by_id[component_id].title,
            standalone_price=_money(products_by_id[component_id].price),
        )
        for component_id in component_ids
    )
    exact_bundle_owned = product.id in owned
    owned_components = tuple(
        component
        for component in components
        if exact_bundle_owned or component.product_id in owned
    )
    remaining_components = tuple(
        component for component in components if component not in owned_components
    )

    full_subtotal = _money(sum(
        (component.standalone_price for component in components),
        start=Decimal(0),
    ))
    owned_subtotal = _money(sum(
        (component.standalone_price for component in owned_components),
        start=Decimal(0),
    ))
    remaining_subtotal = _money(sum(
        (component.standalone_price for component in remaining_components),
        start=Decimal(0),
    ))

    # Invalid catalog data must never create a negative price or negative
    # savings. A price above the standalone subtotal is treated as no discount.
    discounted_catalog_price = min(catalog_price, full_subtotal)
    price_multiplier = (
        discounted_catalog_price / full_subtotal
        if full_subtotal > ZERO_MONEY
        else Decimal(0)
    )
    discount_rate = max(Decimal(0), Decimal(1) - price_multiplier)

    all_components_owned = not remaining_components
    eligible = bool(product.is_active and not exact_bundle_owned and not all_components_owned)
    if exact_bundle_owned or all_components_owned:
        ownership_credit = discounted_catalog_price
        personalized_price = ZERO_MONEY
    else:
        # Round the remaining offer first so a positive residual course cannot
        # become free merely because its discounted share lands on half a cent.
        # Deriving the credit afterward keeps the displayed figures reconciled.
        personalized_price = _money(remaining_subtotal * price_multiplier)
        personalized_price = min(
            personalized_price,
            remaining_subtotal,
            discounted_catalog_price,
        )
        if discounted_catalog_price > ZERO_MONEY and remaining_subtotal > ZERO_MONEY:
            personalized_price = max(personalized_price, MONEY_QUANTUM)
        ownership_credit = _money(discounted_catalog_price - personalized_price)

    savings = _money(max(remaining_subtotal - personalized_price, Decimal(0)))
    reason = (
        "product_already_owned" if exact_bundle_owned
        else "all_components_already_owned" if all_components_owned
        else "product_inactive" if not product.is_active
        else None
    )
    return PersonalizedProductOffer(
        product_id=product.id,
        is_bundle=True,
        eligible=eligible,
        eligibility_reason=reason,
        catalog_price=catalog_price,
        full_standalone_subtotal=full_subtotal,
        owned_standalone_subtotal=owned_subtotal,
        remaining_standalone_subtotal=remaining_subtotal,
        ownership_credit=ownership_credit,
        personalized_price=personalized_price if eligible else ZERO_MONEY,
        savings=savings if eligible else ZERO_MONEY,
        discount_rate=discount_rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP),
        discount_percent=int(
            (discount_rate * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        ),
        owned_components=owned_components,
        remaining_components=remaining_components,
    )
