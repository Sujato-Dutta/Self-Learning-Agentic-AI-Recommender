from fastapi import APIRouter, HTTPException

from src.dependencies import CurrentUser, DbSession
from src.models import Product

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("")
def catalog(db: DbSession, user: CurrentUser, q: str = "", category: str = "", difficulty: str = ""):
    from src.repositories.products import list_products
    products = list_products(db)
    if q:
        needle = q.lower()
        products = [p for p in products if needle in
                    f"{p.title} {p.description} {p.skill_bundle} {' '.join(p.skills)}".lower()]
    if category:
        products = [p for p in products if p.category == category]
    if difficulty:
        products = [p for p in products if p.difficulty == difficulty]
    return [{"id": p.id, "title": p.title, "slug": p.slug, "category": p.category,
             "difficulty": p.difficulty, "price": float(p.price),
             "original_price": float(p.original_price) if p.original_price else None,
             "skill_bundle": p.skill_bundle, "skills": p.skills,
             "content_sections": p.content_sections, "image_url": p.image_url, "is_bundle": p.is_bundle,
             "bundled_product_ids": p.bundled_product_ids,
             "prerequisite_product_ids": p.prerequisite_product_ids,
             "career_outcomes": p.career_outcomes,
             "social_proof_text": p.social_proof_text,
             "promotion_ends_at": p.promotion_ends_at} for p in products]


@router.get("/{product_id}")
def product_detail(product_id: str, db: DbSession, user: CurrentUser):
    product = db.get(Product, product_id)
    if not product or not product.is_active:
        raise HTTPException(404, "Course not found")
    return {"id": product.id, "title": product.title, "description": product.description,
            "category": product.category, "difficulty": product.difficulty, "price": float(product.price),
            "original_price": float(product.original_price) if product.original_price else None,
            "duration_hours": product.duration_hours, "skill_bundle": product.skill_bundle,
            "skills": product.skills, "content_sections": product.content_sections, "tags": product.tags,
            "image_url": product.image_url, "is_bundle": product.is_bundle,
            "bundled_product_ids": product.bundled_product_ids,
            "prerequisite_product_ids": product.prerequisite_product_ids,
            "career_outcomes": product.career_outcomes,
            "social_proof_text": product.social_proof_text,
            "promotion_ends_at": product.promotion_ends_at}
