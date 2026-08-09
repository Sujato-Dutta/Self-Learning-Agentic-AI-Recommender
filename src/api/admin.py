
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from src.dependencies import CurrentAdmin, DbSession
from src.models import AgentRun, Event, Product
from src.repositories.products import archive_product, create_product, update_product
from src.schemas.products import ProductInput
from src.security import validate_csrf

router = APIRouter(prefix="/admin", tags=["admin"])


def form_product(title: str, description: str, category: str, difficulty: str, price: float,
                 duration_hours: float, skills: str, tags: str, image_url: str,
                 original_price: float | None = None, is_bundle: bool = False,
                 bundled_product_ids: str = "", prerequisite_product_ids: str = "",
                 career_outcomes: str = "", social_proof_text: str = "",
                 promotion_ends_at: str = "", skill_bundle: str = "",
                 content_sections: str = "") -> ProductInput:
    component_ids = [x.strip() for x in bundled_product_ids.split(",") if x.strip()]
    sections = [section.strip() for section in re.split(r"\r?\n\s*\r?\n", content_sections) if section.strip()]
    return ProductInput(title=title, description=description, category=category, difficulty=difficulty,
                        price=price, original_price=original_price, duration_hours=duration_hours,
                        skill_bundle=skill_bundle or None,
                        skills=[x.strip() for x in skills.split(",") if x.strip()],
                        content_sections=sections or None,
                        tags=[x.strip() for x in tags.split(",") if x.strip()], image_url=image_url,
                        is_bundle=is_bundle,
                        bundled_product_ids=component_ids if component_ids or not is_bundle else None,
                        prerequisite_product_ids=[x.strip() for x in prerequisite_product_ids.split(",") if x.strip()],
                        career_outcomes=[x.strip() for x in career_outcomes.split(",") if x.strip()],
                        social_proof_text=social_proof_text or None,
                        promotion_ends_at=promotion_ends_at or None)


@router.post("/products")
def create(request: Request, admin: CurrentAdmin, db: DbSession, csrf_token: str = Form(...),
           title: str = Form(...), description: str = Form(...), category: str = Form(...),
           difficulty: str = Form(...), price: float = Form(...), duration_hours: float = Form(...),
           skills: str = Form(""), tags: str = Form(""), image_url: str = Form(""),
           original_price: float | None = Form(None), is_bundle: bool = Form(False),
           bundled_product_ids: str = Form(""), prerequisite_product_ids: str = Form(""),
           career_outcomes: str = Form(""), social_proof_text: str = Form(""),
           promotion_ends_at: str = Form(""), skill_bundle: str = Form(""),
           content_sections: str = Form("")):
    validate_csrf(request, csrf_token)
    try:
        create_product(db, form_product(title, description, category, difficulty, price, duration_hours, skills, tags,
                                        image_url, original_price, is_bundle, bundled_product_ids,
                                        prerequisite_product_ids, career_outcomes, social_proof_text, promotion_ends_at,
                                        skill_bundle, content_sections))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    request.app.state.cache.delete_prefix("catalog:")
    return RedirectResponse("/admin?notice=Course+created+and+vector+sync+queued", status_code=303)


@router.post("/products/{product_id}")
def update(product_id: str, request: Request, admin: CurrentAdmin, db: DbSession, csrf_token: str = Form(...),
           title: str = Form(...), description: str = Form(...), category: str = Form(...),
           difficulty: str = Form(...), price: float = Form(...), duration_hours: float = Form(...),
           skills: str = Form(""), tags: str = Form(""), image_url: str = Form(""),
           original_price: float | None = Form(None), is_bundle: bool = Form(False),
           bundled_product_ids: str = Form(""), prerequisite_product_ids: str = Form(""),
           career_outcomes: str = Form(""), social_proof_text: str = Form(""),
           promotion_ends_at: str = Form(""), skill_bundle: str = Form(""),
           content_sections: str = Form("")):
    validate_csrf(request, csrf_token)
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Course not found")
    try:
        update_product(db, product, form_product(title, description, category, difficulty, price, duration_hours, skills,
                                                 tags, image_url, original_price, is_bundle, bundled_product_ids,
                                                 prerequisite_product_ids, career_outcomes, social_proof_text,
                                                 promotion_ends_at, skill_bundle, content_sections))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/admin?notice=Course+updated+and+vector+sync+queued", status_code=303)


@router.post("/products/{product_id}/archive")
def archive(product_id: str, request: Request, admin: CurrentAdmin, db: DbSession, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Course not found")
    archive_product(db, product)
    return RedirectResponse("/admin?notice=Course+archived", status_code=303)


@router.post("/outbox/{outbox_id}/retry")
def retry_outbox(outbox_id: str, request: Request, admin: CurrentAdmin, db: DbSession, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    request.app.state.outbox.retry(db, outbox_id)
    return RedirectResponse("/admin?notice=Sync+retry+queued", status_code=303)


@router.post("/reconcile")
def reconcile(request: Request, admin: CurrentAdmin, db: DbSession, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    request.app.state.outbox.reconcile(db)
    return RedirectResponse("/admin?notice=Reconciliation+completed", status_code=303)


@router.post("/reset-demo")
def reset_demo(request: Request, admin: CurrentAdmin, db: DbSession, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    from src.seed import seed_database
    seed_database(db, reset_demo=True)
    return RedirectResponse("/admin?notice=Demo+journey+reset", status_code=303)


@router.get("/api/replay/{user_id}")
def replay(user_id: str, admin: CurrentAdmin, db: DbSession):
    events = list(db.scalars(select(Event).where(Event.user_id == user_id).order_by(Event.occurred_at)))
    runs = list(db.scalars(select(AgentRun).where(AgentRun.user_id == user_id).order_by(AgentRun.created_at)))
    return {"events": [{"id": e.event_id, "type": e.event_type, "product_id": e.product_id,
                        "query": e.search_query, "at": e.occurred_at} for e in events],
            "runs": [{"id": r.id, "trigger": r.trigger_type, "status": r.status, "query": r.retrieval_query,
                      "candidates": r.candidates, "trace": r.node_trace, "latency_ms": r.latency_ms,
                      "cache_hit": r.cache_hit, "mesh_called": r.mesh_called} for r in runs]}
