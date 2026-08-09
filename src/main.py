import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, update

from src.api import admin, auth, enrollments, events, health, products, recommendations
from src.config import get_settings
from src.database import Base, SessionLocal, apply_sqlite_catalog_migration, engine
from src.jobs.scheduler import start_scheduler
from src.models import (
    AgentRun,
    Event,
    MarketSignal,
    NextBestAction,
    NotificationPreference,
    OutboxStatus,
    Product,
    ProductSyncFailure,
    ProductVectorOutbox,
    Recommendation,
    ScheduledDelivery,
    User,
)
from src.observability.logging import configure_logging, logger
from src.observability.metrics import HTTP_ACTIVE, HTTP_LATENCY, HTTP_REQUESTS
from src.repositories.enrollments import (
    list_learning_enrollments,
    purchased_product_ids,
)
from src.repositories.products import list_products
from src.security import create_csrf_token, read_session_token
from src.services.behavior_service import aggregate_profile, should_trigger
from src.services.bundle_pricing_service import (
    PersonalizedProductOffer,
    calculate_personalized_offer,
    is_product_offer_eligible,
)
from src.services.cache_service import cache
from src.services.embedding_service import EmbeddingService
from src.services.mesh_client import MeshClient
from src.services.notification_service import NotificationService
from src.services.outbox_service import OutboxService
from src.services.ownership_service import (
    is_product_recommendable,
    product_conflicts_with_ownership,
    related_unowned_products,
)
from src.services.recommendation_service import RecommendationService
from src.services.retrieval_service import RetrievalService
from src.services.skill_taxonomy import SKILL_BUNDLES
from src.services.vector_service import VectorService

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "frontend" / "templates"))
rate_windows: defaultdict[str, deque[float]] = defaultdict(deque)


def format_inr(value) -> str:
    """Format exact rupee amounts compactly while preserving paise."""
    amount = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    if amount == amount.to_integral_value():
        return f"{amount:,.0f}"
    return f"{amount:,.2f}"


templates.env.filters["inr"] = format_inr


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    settings.validate_production()
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.app_env != "production":
        Base.metadata.create_all(engine)
        apply_sqlite_catalog_migration()
        from src.seed import seed_database
        with SessionLocal() as db:
            seed_database(db)
    mesh = MeshClient(settings)
    embeddings = EmbeddingService(mesh)
    vectors = VectorService(settings, embeddings)
    retrieval = RetrievalService(vectors)
    recommendation_service = RecommendationService(settings, mesh, retrieval)
    outbox = OutboxService(settings, vectors)
    notifications = NotificationService(settings)
    app.state.settings = settings
    app.state.mesh = mesh
    app.state.vectors = vectors
    app.state.retrieval = retrieval
    app.state.recommendations = recommendation_service
    app.state.outbox = outbox
    app.state.cache = cache
    app.state.scheduler = start_scheduler(settings, outbox, recommendation_service, notifications)
    logger.info("application_started", environment=settings.app_env)
    yield
    if app.state.scheduler:
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="SmartReco", version="1.0.0", lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "frontend" / "static")), name="static")
app.mount("/assets", StaticFiles(directory=str(ROOT / "assets")), name="assets")
app.include_router(auth.router)
app.include_router(enrollments.router)
app.include_router(events.router)
app.include_router(recommendations.router)
app.include_router(products.router)
app.include_router(admin.router)
app.include_router(health.router)


def current_user(request: Request, db) -> User | None:
    token = request.cookies.get("session")
    payload = read_session_token(token) if token else None
    return db.get(User, payload.get("sub")) if payload else None


def page_context(request: Request, user: User | None = None, **kwargs) -> dict:
    csrf = request.cookies.get("csrf_token") or create_csrf_token()
    return {"request": request, "user": user, "csrf_token": csrf, **kwargs}


def _personalized_product_offers(
    db,
    products,
    owned_product_ids: set[str],
) -> dict[str, PersonalizedProductOffer]:
    """Build one server-authoritative display quote per catalog product."""
    product_list = list(products)
    component_ids = {
        component_id
        for product in product_list
        if product.is_bundle
        for component_id in (product.bundled_product_ids or [])
    }
    components_by_id = {
        component.id: component
        for component in db.scalars(select(Product).where(Product.id.in_(component_ids)))
    } if component_ids else {}
    return {
        product.id: calculate_personalized_offer(
            product,
            [
                components_by_id[component_id]
                for component_id in (product.bundled_product_ids or [])
                if component_id in components_by_id
            ],
            owned_product_ids,
        )
        for product in product_list
    }


def template_response(request: Request, name: str, context: dict, status_code: int = 200):
    response = templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", context["csrf_token"], max_age=604800, httponly=False,
                            secure=get_settings().session_cookie_secure, samesite="lax")
    return response


def _landing_featured_product(
    product: Product,
    signals: list[MarketSignal],
    offer: PersonalizedProductOffer,
    *,
    substituted: bool,
) -> dict:
    """Build the catalog-grounded copy used by the landing Journey Twin demo."""
    skills = list(product.skills or [])
    product_terms = {
        value.casefold()
        for value in [product.skill_bundle, product.category, *skills]
        if value
    }
    matching_signal = max(
        signals,
        key=lambda signal: len(product_terms.intersection(
            skill.casefold() for skill in (signal.skills or [])
        )),
        default=None,
    )
    if matching_signal and not product_terms.intersection(
        skill.casefold() for skill in (matching_signal.skills or [])
    ):
        matching_signal = None

    if product.slug == "agentic-ai-professional" and not substituted:
        interests = ["AI Agents", "RAG", "LLMs"]
        skills_to_build = ["AI Agents", "Transformers", "RAG"]
        if offer.has_ownership_credit:
            owned_count = len(offer.owned_components)
            remaining_count = len(offer.remaining_components)
            dialogue_heading = "I’d complete the production track without paying twice."
            dialogue = (
                f"You already own {owned_count} course{'s' if owned_count != 1 else ''} in this "
                f"track. Your price now covers only the {remaining_count} course"
                f"{'s' if remaining_count != 1 else ''} still missing, with an ownership credit "
                "applied before checkout."
            )
        else:
            dialogue_heading = "I’d move from agent prototypes to a complete production track."
            dialogue = (
                "You searched for Agentic AI, saved Production RAG Systems, and spent time comparing "
                "advanced agents. That combination points to a production gap—not a need to repeat "
                "Python foundations."
            )
        market_claim = "AI Agents was identified as the fastest-growing AI skill of 2025."
    else:
        interests = list(dict.fromkeys([product.skill_bundle, product.category, *skills]))[:3]
        skills_to_build = skills[:3] or [product.skill_bundle]
        item_kind = "track" if product.is_bundle else "course"
        dialogue_heading = f"I’d take {product.title} as the next {product.skill_bundle} step."
        dialogue = (
            f"The original track is already covered by learning you own. This {product.difficulty} "
            f"{item_kind} builds {', '.join(skills_to_build)} and keeps the next move focused on "
            "genuinely new value."
        )
        market_claim = matching_signal.claim if matching_signal else (
            f"{product.title} covers {', '.join(skills_to_build)} across "
            f"{product.duration_hours:g} focused learning hours."
        )

    return {
        "id": product.id,
        "slug": product.slug,
        "title": product.title,
        "price": offer.personalized_price,
        "catalog_price": offer.catalog_price,
        "original_price": offer.remaining_standalone_subtotal,
        "savings": offer.savings,
        "savings_percent": offer.discount_percent,
        "ownership_credit": offer.ownership_credit,
        "owned_component_count": len(offer.owned_components),
        "remaining_component_count": len(offer.remaining_components),
        "owned_component_ids": [component.product_id for component in offer.owned_components],
        "remaining_component_ids": [component.product_id for component in offer.remaining_components],
        "has_ownership_credit": offer.has_ownership_credit,
        "skills": skills,
        "image_url": product.image_url,
        "difficulty": product.difficulty,
        "is_bundle": product.is_bundle,
        "bundled_product_ids": product.bundled_product_ids,
        "action_type": "offer_bundle" if product.is_bundle else "recommend_course",
        "cover_alt": f"{product.title} {'bundle' if product.is_bundle else 'course'} cover",
        "journey_interests": interests,
        "journey_skills": skills_to_build,
        "dialogue_heading": dialogue_heading,
        "dialogue": dialogue,
        "market_label": "MARKET RESEARCH" if matching_signal else "CATALOG EVIDENCE",
        "market_claim": market_claim,
        "market_source_name": matching_signal.source_name if matching_signal else None,
        "market_source_url": matching_signal.source_url if matching_signal else None,
    }


@app.middleware("http")
async def production_middleware(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:100]
    structlog.contextvars.bind_contextvars(request_id=request_id)
    HTTP_ACTIVE.inc()
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = rate_windows[client]
    while window and window[0] < now - 60:
        window.popleft()
    limit = 180 if request.url.path.startswith("/api/events") else 300
    if len(window) >= limit:
        response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    else:
        window.append(now)
        response = await call_next(request)
    elapsed = time.perf_counter() - started
    HTTP_ACTIVE.dec()
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    HTTP_REQUESTS.labels(method=request.method, path=path, status=response.status_code).inc()
    HTTP_LATENCY.labels(path=path).observe(elapsed)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = (
        "no-referrer"
        if request.url.path.startswith("/unsubscribe/")
        else "strict-origin-when-cross-origin"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    structlog.contextvars.clear_contextvars()
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            jsonable_encoder({"detail": "Invalid request", "errors": exc.errors()}),
            status_code=422,
        )
    return template_response(request, "error.html", page_context(request, title="Check your input",
                             message="One or more values need your attention."), 422)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return template_response(request, "error.html", page_context(request, title="Something went wrong",
                             message=str(exc.detail)), exc.status_code)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.exception("unhandled_request_error", path=request.url.path, error_type=type(exc).__name__)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "The service could not complete this request."}, status_code=500)
    return template_response(request, "error.html", page_context(request, title="We hit a snag",
                             message="The service could not complete that request. Please try again."), 500)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_digest(request: Request, token: str):
    """Disable digest delivery without revealing whether the token was valid."""
    with SessionLocal() as db:
        db.execute(
            update(NotificationPreference)
            .where(NotificationPreference.unsubscribe_token == token)
            .values(email_enabled=False)
        )
        db.commit()
    response = template_response(
        request,
        "error.html",
        page_context(
            request,
            title="Email preferences updated",
            message=(
                "If this address was subscribed, its SmartReco recommendation "
                "digest is now disabled."
            ),
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    featured_track = None
    owned_ids: set[str] = set()
    with SessionLocal() as db:
        user = current_user(request, db)
        if user:
            owned_ids = purchased_product_ids(db, user.id)
        active_catalog = list(db.scalars(select(Product).where(Product.is_active.is_(True))))
        product = next(
            (item for item in active_catalog if item.slug == "agentic-ai-professional"),
            None,
        )
        substituted = False
        if product and not is_product_offer_eligible(product, owned_ids):
            alternatives = related_unowned_products(
                active_catalog,
                product,
                owned_ids,
                limit=1,
            )
            product = alternatives[0] if alternatives else None
            substituted = product is not None
        elif not product:
            alternatives = [
                item for item in active_catalog
                if is_product_offer_eligible(item, owned_ids)
            ]
            product = max(
                alternatives,
                key=lambda item: (float(item.popularity), item.title),
                default=None,
            )
            substituted = product is not None
        if product:
            featured_offer = _personalized_product_offers(db, [product], owned_ids)[product.id]
            now = datetime.now(timezone.utc)
            signals = list(db.scalars(select(MarketSignal).where(
                MarketSignal.is_active.is_(True),
                MarketSignal.valid_until >= now,
            ).order_by(MarketSignal.published_at.desc())))
            featured_track = _landing_featured_product(
                product,
                signals,
                featured_offer,
                substituted=substituted,
            )
    return template_response(
        request,
        "landing.html",
        page_context(
            request,
            user,
            featured_track=featured_track,
            purchased_product_ids=sorted(owned_ids),
        ),
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
    if user:
        return RedirectResponse("/admin" if user.role.value == "admin" else "/discover", status_code=303)
    return template_response(request, "login.html", page_context(request, error=request.query_params.get("error")))


@app.get("/discover", response_class=HTMLResponse)
def discover(request: Request, q: str = "", category: str = "", difficulty: str = ""):
    q = q.strip()
    category = category.strip()
    difficulty = difficulty.strip()
    filters_active = bool(q or category or difficulty)
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        owned_ids = purchased_product_ids(db, user.id)
        all_products = list_products(db)
        product_offers = _personalized_product_offers(db, all_products, owned_ids)
        filtered = all_products
        if q:
            needle = q.lower()
            filtered = [p for p in filtered if needle in
                        f"{p.title} {p.description} {p.skill_bundle} {' '.join(p.skills)}".lower()]
        if category:
            filtered = [p for p in filtered if p.category == category]
        if difficulty:
            filtered = [p for p in filtered if p.difficulty == difficulty]
        profile = aggregate_profile(db, user.id)
        recommendation = db.scalar(select(Recommendation).where(Recommendation.user_id == user.id)
                                   .order_by(Recommendation.created_at.desc()))
        action = recommendation.next_best_action if recommendation else None
        base_active = bool(
            recommendation
            and recommendation.status == "active"
            and action
            and action.status not in {
                "dismissed", "converted", "expired", "cancelled", "failed", "delivered",
            }
        )
        if recommendation:
            now = datetime.now(timezone.utc)
            expiry = recommendation.expires_at
            expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
            base_active = base_active and expiry > now
            if action:
                action_expiry = action.expires_at
                action_expiry = (
                    action_expiry
                    if action_expiry.tzinfo
                    else action_expiry.replace(tzinfo=timezone.utc)
                )
                base_active = base_active and action_expiry > now
        ownership_ineligible = bool(
            base_active
            and recommendation
            and (
                (action.product and product_conflicts_with_ownership(action.product, owned_ids))
                or any(
                    product_conflicts_with_ownership(item.product, owned_ids)
                    for item in recommendation.items
                )
            )
        )
        active = bool(
            base_active
            and action
            and (not action.product or is_product_recommendable(action.product, owned_ids))
            and all(
                is_product_recommendable(item.product, owned_ids)
                for item in recommendation.items
            )
        )
        trigger = "no_meaningful_change"
        should_generate = False
        if user.personalization_enabled:
            if not active:
                recent_types = list(db.scalars(select(Event.event_type).where(
                    Event.user_id == user.id,
                ).order_by(Event.occurred_at.desc()).limit(100)))
                should_generate, trigger = should_trigger(profile, recent_types, False)
            elif recommendation and recommendation.profile_hash != profile["profile_hash"]:
                recent_types = list(db.scalars(select(Event.event_type).where(
                    Event.user_id == user.id, Event.occurred_at > recommendation.created_at,
                ).order_by(Event.occurred_at.desc()).limit(100)))
                should_generate, trigger = should_trigger(profile, recent_types, True)
        if should_generate:
            stored_recommendation = recommendation
            try:
                generated = request.app.state.recommendations.generate(
                    db, user.id, f"discover_{trigger}"
                )
                recommendation = generated or (
                    stored_recommendation if ownership_ineligible else None
                )
            except Exception:  # noqa: BLE001 - the page must remain usable in degraded mode
                recommendation = stored_recommendation if ownership_ineligible else None
        elif not active and not ownership_ineligible:
            recommendation = None
        recommendation_data = recommendations.serialize_recommendation(
            recommendation,
            db,
            excluded_product_ids=owned_ids,
            profile=profile,
            minimum_standalone=3,
        ) if recommendation else None
        nba_data = recommendation_data.get("next_best_action") if recommendation_data else None
        nba_product = nba_data.get("product") if nba_data else None
        nba_product_id = nba_product.get("id") if nba_product else None
        nba_offer = product_offers.get(nba_product_id)
        standalone_products = [product for product in filtered if not product.is_bundle]
        bundle_products = [product for product in filtered if product.is_bundle]
        notification_preference = db.get(NotificationPreference, user.id)
        categories = sorted({p.category for p in all_products})
        return template_response(request, "discover.html", page_context(
            request, user, products=standalone_products, bundles=bundle_products,
            result_count=len(filtered), all_products=all_products, categories=categories,
            profile=profile, recommendation=recommendation_data, q=q, selected_category=category,
            selected_difficulty=difficulty, filters_active=filters_active,
            purchased_product_ids=sorted(owned_ids),
            product_offers=product_offers, nba_offer=nba_offer,
            notification_preference=notification_preference,
        ))


@app.get("/courses/{slug}", response_class=HTMLResponse)
def course_detail(slug: str, request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        owned_ids = purchased_product_ids(db, user.id)
        product = db.scalar(select(Product).where(Product.slug == slug, Product.is_active.is_(True)))
        if not product:
            raise HTTPException(404, "Course not found")
        active_catalog = list(db.scalars(select(Product).where(Product.is_active.is_(True))))
        product_offers = _personalized_product_offers(db, active_catalog, owned_ids)
        related = related_unowned_products(
            active_catalog,
            product,
            owned_ids,
            limit=3,
        )
        bundled_products = []
        if product.is_bundle and product.bundled_product_ids:
            bundled_products = list(db.scalars(select(Product).where(Product.id.in_(product.bundled_product_ids))))
            bundled_products.sort(key=lambda item: product.bundled_product_ids.index(item.id))
        content_sections = product.content_sections or [
            product.description,
            f"This {product.duration_hours:g}-hour path connects {', '.join(product.skills)} through guided practice.",
            f"Applied work helps you demonstrate a connected {product.skill_bundle} skill set.",
        ]
        return template_response(request, "course.html", page_context(
            request, user, product=product, related=related, bundled_products=bundled_products,
            content_sections=content_sections, is_purchased=product.id in owned_ids,
            purchased_product_ids=sorted(owned_ids),
            product_offer=product_offers[product.id], product_offers=product_offers,
        ))


@app.get("/learning", response_class=HTMLResponse)
def my_learning(request: Request):
    """Backward-compatible alias for the learner's purchased library."""
    return RedirectResponse("/my-courses", status_code=303)


@app.get("/my-courses", response_class=HTMLResponse)
def my_courses(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        enrollment_rows = list_learning_enrollments(db, user.id)
        owned_ids = purchased_product_ids(db, user.id)
        profile = aggregate_profile(db, user.id)
        return template_response(request, "my_courses.html", page_context(
            request,
            user,
            purchased_products=[enrollment.product for enrollment in enrollment_rows],
            purchased_product_ids=sorted(owned_ids),
            profile=profile,
            active_page="my_courses",
        ))


@app.get("/saved", response_class=HTMLResponse)
def saved_courses(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        owned_ids = purchased_product_ids(db, user.id)
        profile = aggregate_profile(db, user.id)
        products = list_products(db)
        return template_response(request, "saved.html", page_context(
            request, user, products=products, profile=profile, active_page="saved",
            purchased_product_ids=sorted(owned_ids),
            product_offers=_personalized_product_offers(db, products, owned_ids),
        ))


@app.get("/cart", response_class=HTMLResponse)
def cart(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        owned_ids = purchased_product_ids(db, user.id)
        profile = aggregate_profile(db, user.id)
        products = list_products(db)
        return template_response(request, "cart.html", page_context(
            request, user, products=products, profile=profile, active_page="cart",
            purchased_product_ids=sorted(owned_ids),
            product_offers=_personalized_product_offers(db, products, owned_ids),
        ))


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if user.role.value != "admin":
            raise HTTPException(403, "Administrator access required")
        counts = {
            "users": db.scalar(select(func.count()).select_from(User)) or 0,
            "products": db.scalar(select(func.count()).select_from(Product).where(Product.is_active.is_(True))) or 0,
            "events": db.scalar(select(func.count()).select_from(Event)) or 0,
            "recommendations": db.scalar(select(func.count()).select_from(Recommendation)) or 0,
            "actions": db.scalar(select(func.count()).select_from(NextBestAction)) or 0,
            "conversions": db.scalar(select(func.count()).select_from(NextBestAction).where(
                NextBestAction.status == "converted"
            )) or 0,
            "suppressed": db.scalar(select(func.count()).select_from(NextBestAction).where(
                NextBestAction.status == "suppressed"
            )) or 0,
            "pending": db.scalar(select(func.count()).select_from(ProductVectorOutbox).where(ProductVectorOutbox.status == OutboxStatus.pending)) or 0,
            "failed": db.scalar(select(func.count()).select_from(ProductSyncFailure)) or 0,
        }
        return template_response(request, "admin.html", page_context(
            request, user, counts=counts, products=list_products(db, include_inactive=True),
            outbox=list(db.scalars(select(ProductVectorOutbox).order_by(ProductVectorOutbox.created_at.desc()).limit(30))),
            events=list(db.scalars(select(Event).order_by(Event.occurred_at.desc()).limit(50))),
            runs=list(db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(20))),
            actions=list(db.scalars(select(NextBestAction).order_by(
                NextBestAction.created_at.desc()
            ).limit(50))),
            deliveries=list(db.scalars(select(ScheduledDelivery).order_by(ScheduledDelivery.scheduled_at.desc()).limit(20))),
            learners=list(db.scalars(select(User).where(User.role == "user"))),
            skill_bundles=list(SKILL_BUNDLES),
            notice=request.query_params.get("notice"),
        ))


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
