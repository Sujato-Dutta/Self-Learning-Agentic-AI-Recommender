import re
from typing import Any


def validate_grounding(
    output: dict[str, Any],
    allowed_product_ids: set[str],
    evidence_ids: set[str],
    allow_empty: bool = False,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(output.get("headline"), str) or not output.get("headline"):
        errors.append("missing headline")
    narrative = output.get("narrative")
    if not isinstance(narrative, str) or not 30 <= len(narrative) <= 700:
        errors.append("narrative length is invalid")
    recommendations = output.get("recommendations")
    if not isinstance(recommendations, list) or (not recommendations and not allow_empty):
        errors.append("recommendations must be a non-empty list")
        return False, errors
    if not recommendations:
        return not errors, errors
    seen: set[str] = set()
    for item in recommendations:
        product_id = item.get("product_id") if isinstance(item, dict) else None
        if product_id not in allowed_product_ids:
            errors.append(f"unverified product id: {product_id}")
        if product_id in seen:
            errors.append(f"duplicate product id: {product_id}")
        seen.add(product_id)
        cited = set(item.get("evidence_event_ids", [])) if isinstance(item, dict) else set()
        if not cited.issubset(evidence_ids):
            errors.append(f"unsupported evidence for product: {product_id}")
        if not isinstance(item.get("reason"), str) or not item.get("reason"):
            errors.append(f"missing reason for product: {product_id}")
    return not errors, errors


def validate_action_copy(
    output: dict[str, Any],
    selected_product_title: str | None,
    allowed_numbers: set[str],
) -> tuple[bool, list[str]]:
    """Reject action copy that changes the selected item or adds numeric claims."""
    errors: list[str] = []
    action_copy = output.get("action_copy")
    if not isinstance(action_copy, dict):
        return False, ["missing action copy"]
    headline = action_copy.get("headline")
    message = action_copy.get("message")
    if not isinstance(headline, str) or not 3 <= len(headline) <= 180:
        errors.append("action headline length is invalid")
    if not isinstance(message, str) or not 20 <= len(message) <= 1200:
        errors.append("action message length is invalid")
    if selected_product_title and isinstance(message, str) and selected_product_title.lower() not in message.lower():
        errors.append("selected product is absent from action copy")
    if isinstance(message, str):
        found_numbers = set(re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", message))
        unsupported = {value.replace(",", "") for value in found_numbers} - {
            value.replace(",", "") for value in allowed_numbers
        }
        if unsupported:
            errors.append("action copy contains an unsupported numeric claim")
    return not errors, errors
