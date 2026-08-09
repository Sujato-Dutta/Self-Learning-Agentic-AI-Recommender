import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models import Product, ProductVectorOutbox
from src.schemas.products import ProductInput
from src.services.skill_taxonomy import canonical_skills, resolve_skill_bundle


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def list_products(db: Session, include_inactive: bool = False) -> list[Product]:
    query = select(Product).order_by(Product.created_at.desc())
    if not include_inactive:
        query = query.where(Product.is_active.is_(True))
    return list(db.scalars(query))


def _validated_values(db: Session, data: ProductInput, current: Product | None = None) -> dict:
    values = data.model_dump(exclude_none=True)
    skill_bundle = data.skill_bundle or (current.skill_bundle if current else None)
    skill_bundle = skill_bundle or resolve_skill_bundle(data.title, data.category, data.skills)
    values["skill_bundle"] = skill_bundle
    values["skills"] = canonical_skills(skill_bundle)
    bundle_changed = bool(current and current.skill_bundle != skill_bundle)
    if not data.content_sections and (not (current and current.content_sections) or bundle_changed):
        values["content_sections"] = [
            data.description,
            (
                f"Across {data.duration_hours:g} focused hours, this course connects "
                f"{', '.join(values['skills'][:-1])}, and {values['skills'][-1]} through guided practice."
            ),
            (
                f"The {data.difficulty} path finishes with applied work that demonstrates a connected "
                f"{skill_bundle} skill set in a portfolio-ready context."
            ),
        ]
    effective_bundle = data.is_bundle if data.is_bundle is not None else bool(current and current.is_bundle)
    component_ids = (data.bundled_product_ids if data.bundled_product_ids is not None
                     else (current.bundled_product_ids if current else [])) or []
    prerequisite_ids = (data.prerequisite_product_ids if data.prerequisite_product_ids is not None
                        else (current.prerequisite_product_ids if current else [])) or []
    referenced_ids = set(component_ids) | set(prerequisite_ids)
    if current and current.id in referenced_ids:
        raise ValueError("a product cannot include or require itself")
    referenced = {product.id: product for product in db.scalars(select(Product).where(
        Product.id.in_(referenced_ids), Product.is_active.is_(True)
    ))} if referenced_ids else {}
    missing = referenced_ids - set(referenced)
    if missing:
        raise ValueError("one or more referenced products do not exist or are archived")
    if effective_bundle:
        if not component_ids:
            raise ValueError("a bundle must include at least one product")
        if any(referenced[product_id].is_bundle for product_id in component_ids):
            raise ValueError("a bundle can contain standalone courses only")
        original_price = sum((referenced[product_id].price for product_id in component_ids), start=0)
        if data.price >= original_price:
            raise ValueError("bundle price must be lower than the sum of its component prices")
        values["original_price"] = original_price
        values["bundled_product_ids"] = component_ids
    elif data.is_bundle is False:
        values["bundled_product_ids"] = []
    return values


def create_product(db: Session, data: ProductInput) -> Product:
    base_slug = slugify(data.title)
    count = db.scalar(select(func.count()).select_from(Product).where(Product.slug.like(f"{base_slug}%"))) or 0
    product = Product(**_validated_values(db, data), slug=base_slug if count == 0 else f"{base_slug}-{count + 1}")
    db.add(product)
    db.flush()
    db.add(ProductVectorOutbox(product_id=product.id, operation="upsert", product_version=product.version))
    db.commit()
    db.refresh(product)
    return product


def update_product(db: Session, product: Product, data: ProductInput) -> Product:
    for field, value in _validated_values(db, data, product).items():
        setattr(product, field, value)
    product.version += 1
    db.add(ProductVectorOutbox(product_id=product.id, operation="upsert", product_version=product.version))
    db.commit()
    db.refresh(product)
    return product


def archive_product(db: Session, product: Product) -> Product:
    product.is_active = False
    product.version += 1
    db.add(ProductVectorOutbox(product_id=product.id, operation="delete", product_version=product.version))
    db.commit()
    return product
