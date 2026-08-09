from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.main import app
from src.models import Product
from src.services.skill_taxonomy import SKILL_BUNDLES


def test_all_supplied_covers_are_seeded_and_courses_have_display_skills(db):
    products = list(db.scalars(select(Product).where(Product.is_bundle.is_(False), Product.is_active.is_(True))))
    seeded_covers = {Path(product.image_url).name for product in products}
    supplied_covers = {path.name for path in (Path("assets") / "course covers").glob("*.png")}

    assert len(products) == 16
    assert seeded_covers == supplied_covers
    assert all(product.skill_bundle in SKILL_BUNDLES for product in products)
    assert all(product.skills == SKILL_BUNDLES[product.skill_bundle] for product in products)
    assert all(len(product.content_sections) in {3, 4} for product in products)


def test_professional_bundles_have_grounded_membership_and_real_discounts(db):
    bundles = list(db.scalars(select(Product).where(Product.is_bundle.is_(True))))

    assert {bundle.title for bundle in bundles} == {
        "Data Science Professional",
        "Agentic AI Professional",
        "Machine Learning Professional",
        "Generative AI Engineer Professional",
    }
    for bundle in bundles:
        components = list(db.scalars(select(Product).where(Product.id.in_(bundle.bundled_product_ids))))
        assert len(components) == len(bundle.bundled_product_ids)
        assert bundle.original_price == sum((product.price for product in components), start=Decimal(0))
        assert bundle.price < bundle.original_price
        assert bundle.savings_percent > 0


def test_discover_and_bundle_detail_render_sales_and_skills():
    with TestClient(app) as client:
        client.get("/login")
        csrf = client.cookies.get("csrf_token")
        client.post("/auth/login", data={"email": "learner@smartreco.dev",
                    "password": "LearnerDemo123!", "csrf_token": csrf})

        discover = client.get("/discover")
        assert discover.status_code == 200
        assert "Power BI Analytics" in discover.text
        assert "PROFESSIONAL BUNDLE" in discover.text
        assert "skill-badges" not in discover.text
        assert "Current direction" not in discover.text
        assert "High-intent signals" not in discover.text
        assert 'id="twin-editor"' not in discover.text
        assert "&#129302;" in discover.text
        assert "Save 30%" in discover.text
        assert "global-search" not in discover.text
        assert "Refresh recommendations" not in discover.text
        assert "discover-journey-button" in discover.text
        assert "journey-arrow" in discover.text
        assert "insight-collapse" not in discover.text
        assert discover.text.count("data-nba-card") == 1
        assert "IF I WERE IN YOUR POSITION" in discover.text
        assert "next-action-ctas" in discover.text
        assert "match-breakdown" in discover.text
        assert 'class="match-separator" aria-hidden="true">—</span>' in discover.text
        assert "journey-market-research" in discover.text
        assert "MARKET RESEARCH" in discover.text
        assert "next-action-evidence" not in discover.text
        assert discover.text.index("next-action-dialogue") < discover.text.index("journey-market-research")
        assert discover.text.index("journey-market-research") < discover.text.index("next-action-product")
        assert discover.text.index('id="recommended-for-you"') < discover.text.index('id="all-courses"')
        assert discover.text.index('id="all-courses"') < discover.text.index('id="exclusive-course-bundles"')
        assert 'data-save>Save for later' not in discover.text
        before_bundles, bundle_markup = discover.text.split('id="exclusive-course-bundles"', 1)
        assert "Generative AI Engineer Professional" not in before_bundles
        assert "Generative AI Engineer Professional" in bundle_markup

        detail = client.get("/courses/generative-ai-engineer-professional")
        assert detail.status_code == 200
        assert "Courses included" in detail.text
        assert "Production RAG Systems" in detail.text
        assert "Advanced Agentic AI" in detail.text
        assert "26,995" in detail.text
        assert "18,999" in detail.text
        assert detail.text.count("<p>") >= 3
        assert "SKILLS COVERED" in detail.text
        assert "skill-badges" not in detail.text


def test_learner_navigation_and_save_interactions_are_wired():
    sidebar = Path("frontend/templates/_learner_sidebar.html").read_text(encoding="utf-8")
    landing = Path("frontend/templates/landing.html").read_text(encoding="utf-8")
    javascript = Path("frontend/static/js/app.js").read_text(encoding="utf-8")
    stylesheet = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert "data-toggle-sidebar" in sidebar
    assert "My Learning" not in sidebar
    assert "collapse-label" not in sidebar
    assert "Learner · Profile" not in sidebar
    assert "learner-name" in sidebar
    assert 'href="/my-courses"' in sidebar
    assert 'class="sidebar-logout"' in sidebar
    assert 'action="/auth/logout"' in sidebar
    assert 'name="csrf_token"' in sidebar
    assert '/assets/logo.png' in sidebar
    assert '/assets/logo.png' in landing
    assert "🧠" not in sidebar + landing
    assert "brand-reco" in sidebar + landing
    assert "smartreco_sidebar_expanded" in javascript
    assert "globalThis.crypto?.randomUUID?.()" in javascript
    assert 'sidebar?.classList.toggle("collapsed", !expanded)' in javascript
    assert 'button.hasAttribute("data-save-heart")' in javascript
    assert 'saved ? "♥" : "♡"' in javascript
    assert ".save-button.saved" in stylesheet
    assert "#ff4057" in stylesheet
    assert ".discover-section .section-row h2{font-size:24px" in stylesheet
    assert "flex:0 0 29px;width:29px;height:29px" in stylesheet
    assert 'arrow.textContent = expanded ? "←" : "→"' in javascript
    assert "smartreco_enrolled" not in javascript
    assert "continue_learning" not in javascript
    assert "data-demo-purchase" in javascript
    assert "border-radius:10px;background:#000" in stylesheet
    assert ".discover-journey-button .journey-arrow" in stylesheet
    assert "transform:none!important;transition:none" in stylesheet
