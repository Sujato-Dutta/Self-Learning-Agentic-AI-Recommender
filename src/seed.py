from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models import (
    BehaviorProfile,
    Event,
    MarketSignal,
    NextBestAction,
    NextBestActionReward,
    NotificationPreference,
    Product,
    ProductVectorOutbox,
    Recommendation,
    Role,
    User,
    UserEnrollment,
)
from src.repositories.products import slugify
from src.security import hash_password
from src.services.skill_taxonomy import canonical_skills

COURSES = [
    {"title": "Python Foundations", "description": "Master modern Python through practical projects, clean program structure, APIs, and automation workflows.", "category": "Programming", "difficulty": "beginner", "price": 1499, "duration": 14, "skill_bundle": "Python Engineering", "tags": ["coding", "foundations"], "image": "python.png", "popularity": .94},
    {"title": "Data Analysis with Python", "description": "Turn real datasets into clear decisions using Python, analytics, visualization, and business reasoning.", "category": "Data Science", "difficulty": "intermediate", "price": 2499, "duration": 18, "skill_bundle": "Data Analytics", "tags": ["data", "portfolio"], "image": "data_analysis_with_python.png", "popularity": .91},
    {"title": "Machine Learning Engineer", "description": "Build, evaluate, and deploy robust machine learning systems with statistically sound production patterns.", "category": "Machine Learning", "difficulty": "intermediate", "price": 4499, "duration": 32, "skill_bundle": "Machine Learning", "tags": ["ml", "production"], "image": "machine_learning.png", "popularity": .93},
    {"title": "Deep Learning Systems", "description": "Understand neural networks deeply and train reliable models with PyTorch and modern architectures.", "category": "Deep Learning", "difficulty": "advanced", "price": 5499, "duration": 36, "skill_bundle": "Deep Learning", "tags": ["deep-learning", "advanced"], "image": "deep_learning.png", "popularity": .89},
    {"title": "Computer Vision in Practice", "description": "Design image pipelines for classification, detection, and visual search with real-world data.", "category": "Computer Vision", "difficulty": "advanced", "price": 4999, "duration": 28, "skill_bundle": "Computer Vision", "tags": ["vision", "projects"], "image": "computer_vision.png", "popularity": .83},
    {"title": "Transformers & Large Language Models", "slug": "transformers-and-large-language-models", "description": "Go from attention fundamentals to adapting transformers and building responsible LLM applications.", "category": "Generative AI", "difficulty": "advanced", "price": 5999, "duration": 30, "skill_bundle": "NLP & LLMs", "tags": ["llm", "nlp"], "image": "transformers_llms.png", "popularity": .97},
    {"title": "Agentic AI Foundations", "description": "Build AI agents with planning, memory, grounded retrieval, structured outputs, and reliable guardrails.", "category": "Agentic AI", "difficulty": "intermediate", "price": 3999, "duration": 20, "skill_bundle": "Agentic AI", "tags": ["agentic", "ai"], "image": "agentic_ai.png", "popularity": .98},
    {"title": "Advanced Agentic AI", "description": "Engineer multi-agent workflows, durable execution, grounded retrieval, and production observability.", "category": "Agentic AI", "difficulty": "advanced", "price": 6499, "duration": 34, "skill_bundle": "Agentic AI", "tags": ["agents", "production"], "image": "advanced_agentic_ai.png", "popularity": .96},
    {"title": "AI Product Management", "description": "Translate customer problems into valuable AI products with experiments, analytics, and responsible delivery.", "category": "Product", "difficulty": "intermediate", "price": 3499, "duration": 16, "skill_bundle": "AI Product", "tags": ["product", "leadership"], "image": "ai_product_management.png", "popularity": .88},
    {"title": "Applied Data Science", "description": "Complete an end-to-end data science portfolio spanning analysis, statistics, modeling, and communication.", "category": "Data Science", "difficulty": "intermediate", "price": 4299, "duration": 40, "skill_bundle": "Data Science", "tags": ["data-science", "career"], "image": "data_science.png", "popularity": .90},
    {"title": "Advanced Deep Learning", "description": "Scale advanced neural-network work with PyTorch, transformer architectures, and reliable training decisions.", "category": "Deep Learning", "difficulty": "advanced", "price": 6499, "duration": 42, "skill_bundle": "Deep Learning", "tags": ["deep-learning", "systems"], "image": "advanced_deep_learning.png", "popularity": .87},
    {"title": "Natural Language Processing", "description": "Build practical text classification, extraction, semantic search, and language-understanding pipelines.", "category": "Generative AI", "difficulty": "intermediate", "price": 4499, "duration": 28, "skill_bundle": "NLP & LLMs", "tags": ["nlp", "language"], "image": "natural_language_processing.png", "popularity": .91},
    {"title": "Power BI Analytics", "description": "Create decision-ready data models and interactive business dashboards with Power BI and clear visual analysis.", "category": "Data Science", "difficulty": "beginner", "price": 2799, "duration": 18, "skill_bundle": "Data Analytics", "tags": ["analytics", "business-intelligence"], "image": "power_bi.png", "popularity": .89},
    {"title": "Production RAG Systems", "description": "Design, evaluate, and operate retrieval-augmented generation systems that keep LLM answers grounded at scale.", "category": "Generative AI", "difficulty": "advanced", "price": 5999, "duration": 30, "skill_bundle": "Generative AI", "tags": ["rag", "production"], "image": "production_rag.png", "popularity": .96},
    {"title": "Self-Improving AI Systems", "description": "Build AI agents that improve through measured feedback, grounded memory, and safe iteration.", "category": "Agentic AI", "difficulty": "advanced", "price": 6999, "duration": 36, "skill_bundle": "Agentic AI", "tags": ["agents", "feedback"], "image": "self_improving_ai_systems.png", "popularity": .92},
    {"title": "Time Series Forecasting", "description": "Forecast demand and business signals with statistics, machine learning, and modern time-series methods.", "category": "Machine Learning", "difficulty": "intermediate", "price": 3999, "duration": 24, "skill_bundle": "Forecasting", "tags": ["forecasting", "data"], "image": "time_series.png", "popularity": .86},
]


BUNDLES = [
    {"title": "Data Science Professional", "description": "A complete data career path from Python and analytics to Power BI and applied data science.", "price": 7999, "skill_bundle": "Data Science", "image": "data_science.png", "components": ["python-foundations", "data-analysis-with-python", "power-bi-analytics", "applied-data-science"]},
    {"title": "Agentic AI Professional", "description": "Build grounded AI agents from foundations through advanced workflows and production RAG.", "price": 11999, "skill_bundle": "Agentic AI", "image": "advanced_agentic_ai.png", "components": ["agentic-ai-foundations", "advanced-agentic-ai", "production-rag-systems"]},
    {"title": "Machine Learning Professional", "description": "An end-to-end ML path spanning machine learning, deep learning, computer vision, and forecasting.", "price": 17999, "skill_bundle": "Machine Learning", "image": "machine_learning.png", "components": ["machine-learning-engineer", "deep-learning-systems", "advanced-deep-learning", "computer-vision-in-practice", "time-series-forecasting"]},
    {"title": "Generative AI Engineer Professional", "description": "Master the GenAI stack across NLP, transformers, production RAG, and foundational-to-advanced agentic AI.", "price": 18999, "skill_bundle": "Generative AI", "image": "transformers_llms.png", "components": ["natural-language-processing", "transformers-and-large-language-models", "production-rag-systems", "agentic-ai-foundations", "advanced-agentic-ai"]},
]


COURSE_ENRICHMENT = {
    "data-analysis-with-python": {
        "prerequisites": ["python-foundations"],
        "outcomes": ["turn raw datasets into decision-ready analysis"],
    },
    "machine-learning-engineer": {
        "prerequisites": ["python-foundations", "data-analysis-with-python"],
        "outcomes": ["build and deploy production machine-learning workflows"],
    },
    "deep-learning-systems": {
        "prerequisites": ["machine-learning-engineer"],
        "outcomes": ["train and evaluate dependable neural-network systems"],
    },
    "advanced-deep-learning": {
        "prerequisites": ["deep-learning-systems"],
        "outcomes": ["scale advanced training and evaluation workflows"],
    },
    "computer-vision-in-practice": {
        "prerequisites": ["deep-learning-systems"],
        "outcomes": ["ship practical computer-vision pipelines"],
    },
    "applied-data-science": {
        "prerequisites": ["data-analysis-with-python"],
        "outcomes": ["build an end-to-end data-science portfolio"],
    },
    "transformers-and-large-language-models": {
        "prerequisites": ["natural-language-processing"],
        "outcomes": ["evaluate and adapt transformer-based applications"],
    },
    "agentic-ai-foundations": {
        "prerequisites": ["python-foundations"],
        "outcomes": ["build reliable tool-using agent workflows"],
    },
    "advanced-agentic-ai": {
        "prerequisites": ["agentic-ai-foundations"],
        "outcomes": ["engineer observable multi-agent systems for production"],
    },
    "production-rag-systems": {
        "prerequisites": ["agentic-ai-foundations"],
        "outcomes": ["design grounded retrieval systems that operate at scale"],
    },
    "self-improving-ai-systems": {
        "prerequisites": ["advanced-agentic-ai"],
        "outcomes": ["build measured feedback loops for safely improving AI systems"],
    },
}


MARKET_SIGNALS = [
    {
        "name": "linkedin-ai-agents-fastest-growing-skill-2025",
        "claim": "LinkedIn's September 2025 AI Labor Market Update identifies AI Agents as the fastest-growing AI skill of 2025.",
        "source_name": "LinkedIn Economic Graph",
        "source_url": "https://economicgraph.linkedin.com/content/dam/me/economicgraph/en-us/PDF/ai-labor-market-update-header-sept-2025.pdf",
        "skills": ["AI Agents", "Agents", "Agentic AI", "Multi-agent Systems"],
        "published_at": datetime(2025, 9, 1, tzinfo=timezone.utc),
        "valid_until": datetime(2026, 9, 30, tzinfo=timezone.utc),
    },
    {
        "name": "wef-ai-big-data-fast-growing-skills-2030",
        "claim": "The World Economic Forum's Future of Jobs Report 2025 lists AI and big data among the fastest-growing skills through 2030.",
        "source_name": "World Economic Forum",
        "source_url": "https://reports.weforum.org/docs/WEF_Future_of_Jobs_Report_2025.pdf",
        "skills": ["AI", "Machine Learning", "Data Science", "Big Data", "LLMs", "RAG"],
        "published_at": datetime(2025, 1, 7, tzinfo=timezone.utc),
        "valid_until": datetime(2030, 12, 31, tzinfo=timezone.utc),
    },
]


def _add_to_vector_outbox(db: Session, product: Product) -> None:
    db.add(ProductVectorOutbox(product_id=product.id, operation="upsert", product_version=product.version))


def _course_content(course: dict) -> list[str]:
    skills = canonical_skills(course["skill_bundle"])
    return [
        course["description"],
        (
            f"Across {course['duration']} focused hours, {course['title']} connects "
            f"{', '.join(skills[:-1])}, and {skills[-1]}. The sequence moves from clear concepts "
            "to guided decisions, so each skill is understood in the context where it is used."
        ),
        (
            f"The {course['difficulty']} learning path uses realistic exercises and an applied capstone. "
            f"By the end, you will be able to explain the core {course['skill_bundle']} workflow, choose "
            "appropriate approaches, and turn the work into evidence for a portfolio or professional project."
        ),
    ]


def _bundle_content(definition: dict, components: list[Product]) -> list[str]:
    titles = ", ".join(product.title for product in components[:-1])
    titles = f"{titles}, and {components[-1].title}" if len(components) > 1 else components[0].title
    skills = canonical_skills(definition["skill_bundle"])
    return [
        definition["description"],
        (
            f"The track combines {titles} in a deliberate sequence. Earlier courses establish the foundation; "
            "later courses turn it into advanced, connected practice without leaving major gaps between topics."
        ),
        (
            f"Together the courses develop {', '.join(skills[:-1])}, and {skills[-1]}. Buying the track keeps "
            "the full progression in one place and costs less than purchasing its verified catalog components separately."
        ),
    ]


def seed_database(db: Session, reset_demo: bool = False, catalog_only: bool = False) -> None:
    learner = None
    if not catalog_only:
        admin = db.scalar(select(User).where(User.email == "admin@smartreco.dev"))
        if not admin:
            admin = User(email="admin@smartreco.dev", password_hash=hash_password("AdminDemo123!"), role=Role.admin)
            db.add(admin)
        learner = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
        if not learner:
            learner = User(email="learner@smartreco.dev", password_hash=hash_password("LearnerDemo123!"), role=Role.user)
            db.add(learner)
            db.flush()
            db.add(NotificationPreference(user_id=learner.id, email_enabled=False, preferred_hour=17, timezone="Asia/Kolkata"))

    for course in COURSES:
        slug = course.get("slug", slugify(course["title"]))
        product = db.scalar(select(Product).where(Product.slug == slug))
        product = product or db.scalar(select(Product).where(Product.title == course["title"]))
        values = {
            "title": course["title"], "slug": slug, "description": course["description"],
            "category": course["category"], "difficulty": course["difficulty"],
            "price": Decimal(course["price"]), "original_price": None,
            "duration_hours": course["duration"], "skill_bundle": course["skill_bundle"],
            "skills": canonical_skills(course["skill_bundle"]),
            "content_sections": _course_content(course), "tags": course["tags"],
            "image_url": f"/assets/course%20covers/{course['image']}",
            "popularity": course["popularity"], "is_bundle": False, "bundled_product_ids": [],
        }
        if not product:
            product = Product(**values)
            db.add(product)
            db.flush()
            _add_to_vector_outbox(db, product)
        elif any(getattr(product, field) != value for field, value in values.items()):
            for field, value in values.items():
                setattr(product, field, value)
            product.version += 1
            _add_to_vector_outbox(db, product)

    # A short-lived development slug omitted "and" from this title. Preserve the
    # row for audit/history, but hide it when the canonical seeded row also exists.
    canonical_transformers = db.scalar(select(Product).where(Product.slug == "transformers-and-large-language-models"))
    legacy_transformers = db.scalar(select(Product).where(Product.slug == "transformers-large-language-models"))
    if canonical_transformers and legacy_transformers and legacy_transformers.is_active:
        legacy_transformers.is_active = False
        legacy_transformers.version += 1
        db.add(ProductVectorOutbox(product_id=legacy_transformers.id, operation="delete",
                                   product_version=legacy_transformers.version))

    products_by_slug = {product.slug: product for product in db.scalars(select(Product))}
    for slug, enrichment in COURSE_ENRICHMENT.items():
        product = products_by_slug.get(slug)
        if not product:
            continue
        prerequisite_ids = [products_by_slug[item].id for item in enrichment["prerequisites"] if item in products_by_slug]
        values = {
            "prerequisite_product_ids": prerequisite_ids,
            "career_outcomes": enrichment["outcomes"],
            "social_proof_text": (
                "This is among the highest-popularity courses in the current SmartReco catalog."
                if product.popularity >= .92 else None
            ),
        }
        if any(getattr(product, field) != value for field, value in values.items()):
            for field, value in values.items():
                setattr(product, field, value)
            product.version += 1
            _add_to_vector_outbox(db, product)

    for definition in BUNDLES:
        components = [products_by_slug[slug] for slug in definition["components"]]
        original_price = sum((product.price for product in components), start=Decimal(0))
        bundled_ids = [product.id for product in components]
        slug = slugify(definition["title"])
        bundle = products_by_slug.get(slug)
        values = {
            "title": definition["title"], "description": definition["description"],
            "category": "Professional Bundle", "difficulty": "all-levels",
            "price": Decimal(definition["price"]), "original_price": original_price,
            "duration_hours": sum(product.duration_hours for product in components),
            "skill_bundle": definition["skill_bundle"],
            "skills": canonical_skills(definition["skill_bundle"]),
            "content_sections": _bundle_content(definition, components),
            "tags": ["bundle", "career-path", "professional"],
            "image_url": f"/assets/course%20covers/{definition['image']}", "popularity": .99,
            "is_bundle": True, "bundled_product_ids": bundled_ids,
            "career_outcomes": [f"complete the connected {definition['title'].lower()} skill path"],
            "social_proof_text": "This bundle combines a complete, catalog-verified professional path.",
        }
        if not bundle:
            bundle = Product(slug=slug, **values)
            db.add(bundle)
            db.flush()
            _add_to_vector_outbox(db, bundle)
            products_by_slug[slug] = bundle
        elif any(getattr(bundle, field) != value for field, value in values.items()):
            for field, value in values.items():
                setattr(bundle, field, value)
            bundle.version += 1
            _add_to_vector_outbox(db, bundle)

    for signal_data in MARKET_SIGNALS:
        signal = db.scalar(select(MarketSignal).where(MarketSignal.name == signal_data["name"]))
        if not signal:
            db.add(MarketSignal(**signal_data))
        else:
            for field, value in signal_data.items():
                setattr(signal, field, value)
            signal.is_active = True

    db.commit()
    if catalog_only:
        return
    assert learner is not None
    if reset_demo:
        db.execute(delete(UserEnrollment).where(UserEnrollment.user_id == learner.id))
        db.execute(delete(Event).where(Event.user_id == learner.id))
        db.execute(delete(BehaviorProfile).where(BehaviorProfile.user_id == learner.id))
        db.execute(delete(NextBestActionReward).where(NextBestActionReward.user_id == learner.id))
        db.execute(delete(NextBestAction).where(NextBestAction.user_id == learner.id))
        db.execute(delete(Recommendation).where(Recommendation.user_id == learner.id))
        db.commit()
    if db.scalar(select(Event.id).where(Event.user_id == learner.id).limit(1)) is None:
        products = {p.title: p for p in db.scalars(select(Product))}
        now = datetime.now(timezone.utc)
        demo = [
            ("demo-search-001", "search", None, "transformers and llms", {}, now - timedelta(minutes=18)),
            ("demo-view-001", "product_view", products["Transformers & Large Language Models"].id, None, {}, now - timedelta(minutes=16)),
            ("demo-dwell-001", "time_spent", products["Transformers & Large Language Models"].id, None, {"dwell_seconds": 260}, now - timedelta(minutes=12)),
            ("demo-wishlist-001", "wishlist_add", products["Agentic AI Foundations"].id, None, {}, now - timedelta(minutes=8)),
            ("demo-cart-001", "cta_click", products["Agentic AI Foundations"].id, None, {"source": "add_to_cart"}, now - timedelta(minutes=6)),
            ("demo-view-002", "product_view", products["Advanced Agentic AI"].id, None, {}, now - timedelta(minutes=4)),
        ]
        for event_id, kind, product_id, query, metadata, at in demo:
            db.add(Event(event_id=event_id, user_id=learner.id, session_id="demo-session-2026",
                         event_type=kind, product_id=product_id, search_query=query,
                         event_metadata=metadata, occurred_at=at))
        db.commit()
