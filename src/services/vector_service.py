from dataclasses import dataclass
from typing import Any

from pinecone import Pinecone, ServerlessSpec

from src.config import Settings
from src.models import Product
from src.services.embedding_service import EmbeddingService


@dataclass
class VectorMatch:
    product_id: str
    score: float
    metadata: dict[str, Any]


class VectorService:
    def __init__(self, settings: Settings, embedding_service: EmbeddingService) -> None:
        self.settings = settings
        self.embedding_service = embedding_service
        self.client = Pinecone(api_key=settings.pinecone_api_key.get_secret_value()) if settings.pinecone_api_key else None
        self._index = None

    @property
    def available(self) -> bool:
        return self.client is not None

    def ensure_index(self) -> None:
        if not self.client:
            raise RuntimeError("Pinecone is not configured")
        names = {index.name for index in self.client.list_indexes()}
        if self.settings.pinecone_index_name not in names:
            self.client.create_index(
                name=self.settings.pinecone_index_name,
                dimension=1536,
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region="us-east-1"),
            )

    @property
    def index(self):
        if self._index is None:
            self.ensure_index()
            self._index = self.client.Index(self.settings.pinecone_index_name)
        return self._index

    def upsert_product(self, product: Product) -> None:
        self.upsert_products([product])

    def upsert_products(self, products: list[Product]) -> None:
        if not products:
            return
        vectors = self.embedding_service.embed_products(products)
        self.index.upsert(
            vectors=[{
                "id": product.id,
                "values": vector,
                "metadata": {
                    "title": product.title, "category": product.category,
                    "difficulty": product.difficulty, "price": float(product.price),
                    "duration_hours": product.duration_hours, "skills": product.skills,
                    "tags": product.tags, "is_active": product.is_active,
                    "skill_bundle": product.skill_bundle, "is_bundle": product.is_bundle,
                    "career_outcomes": product.career_outcomes or [],
                    "prerequisite_product_ids": product.prerequisite_product_ids or [],
                    "version": product.version,
                },
            } for product, vector in zip(products, vectors, strict=True)],
            namespace=self.settings.pinecone_namespace,
        )

    def delete_product(self, product_id: str) -> None:
        self.delete_products([product_id])

    def delete_products(self, product_ids: list[str]) -> None:
        if product_ids:
            self.index.delete(ids=product_ids, namespace=self.settings.pinecone_namespace)

    def query(self, query: str, top_k: int = 30, filters: dict | None = None) -> list[VectorMatch]:
        vector = self.embedding_service.embed_query(query)
        result = self.index.query(
            vector=vector,
            top_k=top_k,
            include_metadata=True,
            namespace=self.settings.pinecone_namespace,
            filter=filters or {"is_active": {"$eq": True}},
        )
        return [VectorMatch(match.id, float(match.score), dict(match.metadata or {})) for match in result.matches]

    def vector_ids(self) -> set[str]:
        vector_ids: set[str] = set()
        for page in self.index.list(namespace=self.settings.pinecone_namespace):
            if isinstance(page, list):
                vector_ids.update(str(item) for item in page)
            elif hasattr(page, "vectors"):
                vector_ids.update(str(item.id) for item in page.vectors)
        return vector_ids
