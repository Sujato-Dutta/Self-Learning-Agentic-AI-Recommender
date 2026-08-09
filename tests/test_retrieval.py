from sqlalchemy import select

from src.models import Product
from src.services.reranking_service import rerank, score_candidate
from src.services.retrieval_service import (
    Candidate,
    build_retrieval_query,
    metadata_filters,
)


def profile():
    return {"primary_goal": "Build Agentic AI systems", "emerging_interests": ["Agentic AI"],
            "skill_gaps": ["LangGraph", "RAG"], "difficulty_preference": "advanced",
            "budget_max": 7000, "excluded_product_ids": [], "saved_product_ids": []}


def test_query_and_metadata_filters_are_grounded():
    query = build_retrieval_query(profile())
    assert "Agentic AI" in query and "LangGraph" in query
    assert metadata_filters(profile())["price"]["$lte"] == 7000


def test_reranker_rewards_constraint_fit(db):
    advanced = db.scalar(select(Product).where(Product.title == "Advanced Agentic AI"))
    python = db.scalar(select(Product).where(Product.title == "Python Foundations"))
    a = score_candidate(Candidate(advanced, .9, "test"), profile())
    b = score_candidate(Candidate(python, .9, "test"), profile())
    assert a.final_score > b.final_score
    assert a.breakdown["difficulty"] == 1


def test_diversity_filter_limits_repeated_categories(db):
    products = list(db.scalars(select(Product)))
    candidates = [Candidate(product, .95 - index * .01, "test") for index, product in enumerate(products)]
    selected = rerank(candidates, profile(), limit=5)
    categories = [item.candidate.product.category for item in selected]
    assert all(categories.count(category) <= 2 for category in set(categories))

