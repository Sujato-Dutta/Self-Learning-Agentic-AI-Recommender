import smtplib
from collections.abc import Iterable, Mapping
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from urllib.parse import quote

from src.config import Settings
from src.models import Recommendation, RecommendationItem, User
from src.services.bundle_pricing_service import PersonalizedProductOffer
from src.services.ownership_service import is_product_recommendable


class NoEligibleRecommendationItems(RuntimeError):
    """Raised when live catalog/ownership state leaves nothing safe to deliver."""


def _format_inr(value) -> str:
    amount = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    return f"{amount:,.0f}" if amount == amount.to_integral_value() else f"{amount:,.2f}"


class NotificationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.smtp_host)

    @staticmethod
    def eligible_items(
        recommendation: Recommendation,
        owned_product_ids: Iterable[str] = (),
    ) -> list[RecommendationItem]:
        """Return email items after revalidating live ownership and catalog state."""
        owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
        action = recommendation.next_best_action
        items = (
            [next((item for item in recommendation.items if item.product_id == action.product_id), None)]
            if action and action.product_id else recommendation.items
        )
        return [
            item for item in items
            if item and is_product_recommendable(item.product, owned)
        ]

    def send_digest(
        self,
        user: User,
        recommendation: Recommendation,
        unsubscribe_token: str,
        *,
        owned_product_ids: Iterable[str] = (),
        personalized_offers: Mapping[str, PersonalizedProductOffer] | None = None,
    ) -> None:
        if not self.settings.smtp_host:
            raise RuntimeError("SMTP is not configured")
        action = recommendation.next_best_action
        owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
        if action and action.product_id and not is_product_recommendable(action.product, owned):
            raise NoEligibleRecommendationItems("the selected action product is no longer eligible")
        items = self.eligible_items(recommendation, owned)
        if (not action or not action.product_id) and not items:
            raise NoEligibleRecommendationItems("the recommendation has no eligible products")

        selections = [(item.product, item.reason) for item in items]
        if not selections and action and action.product:
            # A policy-selected course or bundle need not also be persisted as a
            # RecommendationItem. Render that direct action instead of sending
            # an empty product list.
            selections = [(action.product, action.rationale or action.message)]

        # The scheduler supplies an authoritative live quote map. Fail closed
        # for a bundle missing from it instead of emailing a stale catalog price
        # after the learner's ownership changes.
        if personalized_offers is not None:
            for product, _reason in selections:
                offer = personalized_offers.get(product.id)
                if product.is_bundle and (not offer or not offer.eligible):
                    raise NoEligibleRecommendationItems(
                        "the selected bundle no longer has a valid personalized offer"
                    )

        def offer_for(product_id: str) -> PersonalizedProductOffer | None:
            return personalized_offers.get(product_id) if personalized_offers is not None else None

        def price_text(product) -> str:
            offer = offer_for(product.id)
            amount = offer.personalized_price if offer else product.price
            if offer and offer.has_ownership_credit:
                return (
                    f"₹{_format_inr(amount)} after "
                    f"₹{_format_inr(offer.ownership_credit)} "
                    "owned-course credit"
                )
            return f"₹{_format_inr(amount)}"

        def product_url(slug: str) -> str:
            base_url = self.settings.app_base_url.rstrip("/")
            safe_slug = quote(slug, safe="")
            return f"{base_url}/courses/{safe_slug}?utm_source=smartreco_digest"

        rows = "".join(
            f'<li><a href="{escape(product_url(product.slug), quote=True)}">'
            f'{escape(product.title)}</a> — '
            f'{"Bundle" if product.is_bundle else "Course"} · {escape(price_text(product))} · '
            f'{escape(reason)}</li>'
            for product, reason in selections
        )
        plain_rows = "\n".join(
            f"- {product.title} — "
            f'{"Bundle" if product.is_bundle else "Course"} · {price_text(product)}\n'
            f"  {product_url(product.slug)}\n"
            f"  {reason}"
            for product, reason in selections
        )
        headline = action.headline if action else recommendation.headline
        narrative = action.message if action else recommendation.narrative
        partial_offers = [
            (product, offer_for(product.id))
            for product, _reason in selections
            if offer_for(product.id) and offer_for(product.id).has_ownership_credit
        ]
        if partial_offers:
            product, offer = partial_offers[0]
            assert offer is not None
            owned_titles = ", ".join(item.title for item in offer.owned_components)
            headline = f"Complete {product.title} without paying twice"
            narrative = (
                f"You already own {owned_titles}. SmartReco removed that discounted share, "
                f"so ₹{_format_inr(offer.personalized_price)} covers only the "
                f"{len(offer.remaining_components)} remaining "
                f"{'course' if len(offer.remaining_components) == 1 else 'courses'} in this track. "
                f"{narrative}"
            )
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = user.email
        message["Subject"] = headline
        message.set_content(f"{narrative}\n\n{plain_rows}")
        message.add_alternative(
            f"<h1>{escape(headline)}</h1><p>{escape(narrative)}</p>"
            f"<ol>{rows}</ol><p><a href='{self.settings.app_base_url}/unsubscribe/{unsubscribe_token}'>Unsubscribe</a></p>",
            subtype="html",
        )
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as smtp:
            if self.settings.smtp_use_tls:
                smtp.starttls()
            if self.settings.smtp_username and self.settings.smtp_password:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password.get_secret_value())
            smtp.send_message(message)
