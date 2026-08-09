from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_landing_matches_the_current_product_architecture():
    template = (ROOT / "frontend/templates/landing.html").read_text(encoding="utf-8")

    assert "Open workspace" not in template
    assert "Designed for modern learning platforms" not in template
    assert "floating-signal" not in template
    assert "hero-video-mask" in template
    assert "Trusted by top companies" in template
    assert "LEARNING ECOSYSTEM" not in template
    logo_files = [
        "scaler_logo.png",
        "pw_logo.png",
        "unacademy_logo_clean.png",
        "udemy_logo.png",
        "coursera_logo.webp",
        "udacity_logo.png",
    ]
    for logo_file in logo_files:
        assert template.count(f"/assets/company%20logos/{logo_file}") == 2
        assert (ROOT / "assets/company logos" / logo_file).is_file()
    assert template.count('class="company-logo-list"') == 2
    assert 'class="company-logo-list" aria-hidden="true"' in template
    marquee = template.split('class="company-marquee"', 1)[1].split("</section>", 1)[0]
    assert "<b>" not in marquee
    assert "Next-best-action intelligence for learning commerce" not in template
    assert "SmartReco observes user behavior to find courses aligned with their interests and job requirements." in template
    hero = template.split('class="hero-content"', 1)[1].split('class="decision-stage"', 1)[0]
    assert "Catalog-grounded" not in hero
    assert "Revenue-aware" not in hero
    assert "Respectfully timed" not in hero
    assert "hero-title-line" in hero
    assert '<br><span class="hero-title-final">Every time.</span>' in hero

    assert 'role="tablist"' in template
    assert template.count('role="tab"') == 6
    assert template.count('role="tabpanel"') == 6
    assert template.count('aria-selected="true"') == 1
    assert "data-feature-autoplay" in template

    dialogue = template.index("landing-twin-dialogue")
    research = template.index("landing-market-research")
    product = template.index("landing-action-product")
    assert dialogue < research < product
    assert "IF I WERE IN YOUR POSITION" in template
    assert "a decisive next step based on market evidence" in template
    assert template.count('class="landing-action-product"') == 1
    assert "twin-orbit" not in template
    assert "Current direction" not in template
    assert "Try a different future" not in template
    assert 'id="studio"' not in template
    assert "/assets/course%20covers/advanced_agentic_ai.png" in template
    assert (ROOT / "assets/course covers/advanced_agentic_ai.png").is_file()
    assert "data-cart-label=\"Add track to cart\"" in template
    assert 'href="/courses/{{ featured_track.slug }}"' in template
    assert "data-component-ids='{{ featured_track.bundled_product_ids|tojson }}'" in template

    for removed_eyebrow in [
        "ONE CONNECTED DECISION SYSTEM",
        "THE SIGNATURE EXPERIENCE",
        "PRODUCTION-AWARE BY DEFAULT",
        "READY FOR THE NEXT MOVE",
    ]:
        assert removed_eyebrow not in template
    assert 'class="landing-cta-accent"' in template
    assert "Turn those signals into one next step worth taking." in template

    pipeline = [
        "Behavior events",
        "Intent + propensity",
        "Candidate retrieval",
        "Reranking",
        "Next-best-action policy",
        "Persuasion selection",
        "Grounded Mesh copy",
        "Reward feedback",
    ]
    positions = [template.index(stage) for stage in pipeline]
    assert positions == sorted(positions)
    assert '<ol class="pipeline-grid" data-decision-graph>' in template
    assert template.count("data-decision-node") == len(pipeline)
    assert "production-principles" not in template
    assert "Truth before pressure" not in template
    assert "Mesh intelligence active" in template
    assert "Adaptive persuasion grounded" in template
    assert "Mesh orchestration" in template
    assert "Grounding validation" in template
    for retired_message in [
        "Deterministic copy active",
        "Mesh spend gate paused",
        "Paused by default",
        "MESH_CALLS_ENABLED",
        "Fallback copy",
    ]:
        assert retired_message not in template
    assert "Profile hash + TTL" in template
    assert "Version-coalescing outbox" in template
    for unsupported_metric in ["12.8K", "78.4%", "99.2%"]:
        assert unsupported_metric not in template


def test_landing_motion_is_accessible_and_resource_aware():
    javascript = (ROOT / "frontend/static/js/app.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "frontend/static/css/app.css").read_text(encoding="utf-8")

    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in javascript
    assert "navigator.connection?.saveData" in javascript
    assert "IntersectionObserver" in javascript
    for key in ["ArrowRight", "ArrowLeft", "Home", "End"]:
        assert key in javascript
    assert "structured_completion" not in javascript
    assert "data-open-studio" not in javascript
    assert "scheduleDecisionGraph" in javascript
    assert 'classList.toggle("is-current", current)' in javascript
    assert 'classList.toggle("is-past", index < activeDecisionNode)' in javascript
    assert 'node.setAttribute("aria-current", "step")' in javascript
    assert "company-marquee:hover" in stylesheet
    assert "company-marquee:focus" in stylesheet
    assert "@media(prefers-reduced-motion:reduce)" in stylesheet
    assert ".hero-video-mask" in stylesheet
    assert ".journey-copy li b{font-size:15px}" in stylesheet
    assert ".journey-copy>.button{min-height:51px" in stylesheet
    assert ".landing-page :is(button,.button):hover:not(:disabled)" in stylesheet
    assert ".landing-page .landing-hero .hero-content>p{font-size:19px}" in stylesheet
    assert ".company-logo{height:76px;padding:0;border:0;border-radius:0;background:transparent" in stylesheet
    assert ".landing-page .landing-hero h1 span.hero-title-line{display:inline-block" in stylesheet
    assert "font-size:1em;line-height:inherit;white-space:nowrap" in stylesheet
    assert "min-height:100svh;height:100svh" in stylesheet
    assert "display:grid;grid-template-columns:minmax(0,1fr) minmax(400px,520px)" in stylesheet
    assert "font-size:clamp(52px,4.2vw,68px);line-height:1.1" in stylesheet
    assert "max-width:600px;font-size:19px;line-height:1.6" in stylesheet
    assert ".decision-stage{position:relative;top:auto;right:auto;justify-self:end" in stylesheet
    assert ".company-logo-list{align-items:flex-end" in stylesheet
    assert ".company-logo{position:relative;align-items:flex-end;height:64px" in stylesheet
    assert ".company-logo.logo-coursera img{top:-48px;width:175px" in stylesheet
    assert ".company-logo.logo-udemy img{top:2px;width:90px" in stylesheet
    assert ".company-trust{grid-template-columns:minmax(250px,290px) 1fr}" in stylesheet
    assert "padding:0 .02em .14em;margin:0 -.02em -.14em" in stylesheet
    assert ".feature-tour-head>span{color:#c9d9e2;font-size:16px" in stylesheet
    assert ".pipeline-grid li.is-current" in stylesheet
    assert ".pipeline-grid li:hover,.pipeline-grid li:focus-visible" in stylesheet
    assert "@keyframes pipeline-current-glow" in stylesheet
    assert ".landing-twin-dialogue p{font-size:13px}" in stylesheet
    assert ".landing-cta-accent{color:var(--green)}" in stylesheet
    assert ".landing-page .landing-cta{align-items:center;justify-content:space-between;flex-direction:row" in stylesheet
    assert ".landing-cta>.button{flex:0 0 auto;align-self:center}" in stylesheet
    assert "letter-spacing:.01em;word-spacing:.06em;white-space:nowrap" in stylesheet
    assert "@media(max-width:1240px)" in stylesheet


def test_documentation_describes_the_mesh_production_path():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example_env = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "MESH_CALLS_ENABLED=true" in readme
    assert "MESH_CALLS_ENABLED=true" in example_env
    for retired_message in [
        "MESH_CALLS_ENABLED=false",
        "Deterministic grounded copy",
        "Persist deterministic copy",
        "Mesh is paused",
        "spending gate",
        "zero-AI simulation",
        "SQL-only simulations",
    ]:
        assert retired_message not in readme
