from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DemoPurchaseIn(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=50)
    source: Literal["cart_checkout", "course_detail_demo_purchase"]

    @field_validator("product_ids")
    @classmethod
    def validate_product_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            product_id = value.strip()
            if not product_id or len(product_id) > 36:
                raise ValueError("product IDs must be non-empty UUID strings")
            if product_id not in normalized:
                normalized.append(product_id)
        if not normalized:
            raise ValueError("at least one product is required")
        return normalized


class DemoPurchaseOfferOut(BaseModel):
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
    owned_component_ids: list[str]
    remaining_component_ids: list[str]


class DemoPurchaseOut(BaseModel):
    purchased_product_ids: list[str]
    enrolled_product_ids: list[str]
    newly_enrolled_product_ids: list[str]
    already_enrolled_product_ids: list[str]
    event_ids: list[str]
    profile_version: int | None
    rewards_attributed: int
    currency: Literal["INR"] = "INR"
    offers: list[DemoPurchaseOfferOut]
    catalog_total: Decimal
    ownership_credit_total: Decimal
    payable_total: Decimal
    savings_total: Decimal
