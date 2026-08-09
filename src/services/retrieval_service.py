import re
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import Product
from src.observability.metrics import CANDIDATE_COUNT, RETRIEVAL_LATENCY
from src.services.ownership_service import is_product_recommendable
from src.services.vector_service import VectorService


@dataclass
class Candidate:
    product: Product
    retrieval_score: float
    source: str


def build_retrieval_query(profile: dict) -> str:
    return "; ".join([
        profile.get("primary_goal", "career growth"),
        "interests: " + ", ".join(profile.get("emerging_interests", [])),
        "managed skill bundles: " + ", ".join(profile.get("emerging_interests", [])),
        "skill gaps: " + ", ".join(profile.get("skill_gaps", [])),
        "difficulty: " + profile.get("difficulty_preference", "intermediate"),
    ])


def metadata_filters(profile: dict) -> dict:
    filters: dict = {"is_active": {"$eq": True}}
    budget = profile.get("budget_max")
    # Static vector metadata cannot represent a learner's ownership credit.
    # With durable purchases, keep potentially affordable completion bundles in
    # the pool and let the DB-backed reranker/policy apply personalized pricing.
    if budget is not None and not profile.get("purchased_product_ids"):
        filters["price"] = {"$lte": float(budget)}
    return filters


class RetrievalService:
    def __init__(self, vector_service: VectorService) -> None:
        self.vector_service = vector_service

    def retrieve(self, db: Session, profile: dict, top_k: int = 30) -> tuple[str, list[Candidate], bool]:
        started = time.perf_counter()
        query = build_retrieval_query(profile)
        excluded = set(profile.get("excluded_product_ids", []))
        owned = set(profile.get("purchased_product_ids", []))
        products = list(db.scalars(select(Product).where(Product.is_active.is_(True))))
        by_id = {p.id: p for p in products}
        degraded = False
        try:
            matches = self.vector_service.query(query, top_k=top_k, filters=metadata_filters(profile))
            candidates = [Candidate(by_id[m.product_id], m.score, "pinecone") for m in matches
                          if m.product_id in by_id and m.product_id not in excluded
                          and is_product_recommendable(by_id[m.product_id], owned)]
            if len(candidates) < top_k:
                selected_ids = {candidate.product.id for candidate in candidates}
                candidates.extend(
                    candidate for candidate in self._lexical_fallback(
                        query, products, excluded | selected_ids, owned
                    ) if candidate.product.id not in selected_ids
                )
                candidates = candidates[:top_k]
        except Exception:  # noqa: BLE001 - any vector-provider failure activates lexical fallback
            degraded = True
            candidates = self._lexical_fallback(query, products, excluded, owned)
        CANDIDATE_COUNT.observe(len(candidates))
        RETRIEVAL_LATENCY.observe(time.perf_counter() - started)
        return query, candidates, degraded

    def preview(self, db: Session, profile: dict, top_k: int = 30) -> tuple[str, list[Candidate]]:
        """Cost-free SQL/lexical retrieval for interactive what-if controls."""
        query = build_retrieval_query(profile)
        products = list(db.scalars(select(Product).where(Product.is_active.is_(True))))
        candidates = self._lexical_fallback(
            query,
            products,
            set(profile.get("excluded_product_ids", [])),
            set(profile.get("purchased_product_ids", [])),
        )
        return query, candidates[:top_k]

    @staticmethod
    def _lexical_fallback(
        query: str,
        products: list[Product],
        excluded: set[str],
        owned: set[str] | None = None,
    ) -> list[Candidate]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        owned = owned or set()
        ranked: list[Candidate] = []
        for product in products:
            if product.id in excluded or not is_product_recommendable(product, owned):
                continue
            text = " ".join([
                product.title, product.description, product.category, product.skill_bundle,
                *product.skills, *product.tags, *(product.content_sections or []),
            ]).lower()
            tokens = set(re.findall(r"[a-z0-9]+", text))
            overlap = len(terms & tokens) / max(len(terms), 1)
            ranked.append(Candidate(product, min(0.92, 0.25 + overlap + product.popularity * 0.15), "lexical_fallback"))
        return sorted(ranked, key=lambda c: c.retrieval_score, reverse=True)
