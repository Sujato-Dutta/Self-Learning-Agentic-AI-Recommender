import hashlib

from src.models import Product
from src.services.cache_service import cache
from src.services.mesh_client import MeshClient


def product_embedding_text(product: Product) -> str:
    return " | ".join([
        product.title,
        product.description,
        f"Category: {product.category}",
        f"Skill bundle: {product.skill_bundle}",
        f"Difficulty: {product.difficulty}",
        "Skills: " + ", ".join(product.skills),
        "Tags: " + ", ".join(product.tags),
        "Career outcomes: " + ", ".join(product.career_outcomes or []),
        "Prerequisites: " + ", ".join(product.prerequisite_product_ids or []),
        "Course content: " + " ".join(product.content_sections or []),
    ])


class EmbeddingService:
    def __init__(self, mesh: MeshClient) -> None:
        self.mesh = mesh

    def embed_product(self, product: Product) -> list[float]:
        return self.embed_products([product])[0]

    def embed_products(self, products: list[Product]) -> list[list[float]]:
        """Embed cache misses in one provider request instead of one per product."""
        results: list[list[float] | None] = [None] * len(products)
        missing_texts: list[str] = []
        missing: list[tuple[int, str]] = []
        for index, product in enumerate(products):
            text = product_embedding_text(product)
            key = f"embedding:{hashlib.sha256(text.encode()).hexdigest()}"
            existing = cache.get(key)
            if existing is not None:
                results[index] = existing
            else:
                missing_texts.append(text)
                missing.append((index, key))
        if missing_texts:
            vectors = self.mesh.embed(missing_texts)
            for (index, key), vector in zip(missing, vectors, strict=True):
                results[index] = vector
                cache.set(key, vector, 86400)
        return [vector for vector in results if vector is not None]

    def embed_query(self, query: str) -> list[float]:
        key = f"embedding:{hashlib.sha256(query.encode()).hexdigest()}"
        existing = cache.get(key)
        if existing is not None:
            return existing
        vector = self.mesh.embed([query])[0]
        cache.set(key, vector, 3600)
        return vector
