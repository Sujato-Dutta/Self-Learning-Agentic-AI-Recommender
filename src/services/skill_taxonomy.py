"""Small, canonical skill vocabulary shared by catalog and behavior models."""

import re
from collections import OrderedDict

SKILL_BUNDLES: dict[str, list[str]] = OrderedDict({
    "Python Engineering": ["Python", "Software Engineering", "APIs", "Automation"],
    "Data Analytics": ["Python", "Analytics", "Power BI", "Visualization"],
    "Data Science": ["Python", "Data Analysis", "Statistics", "Machine Learning"],
    "Machine Learning": ["Python", "Machine Learning", "Statistics", "Neural Networks"],
    "Deep Learning": ["PyTorch", "Neural Networks", "Deep Learning", "Transformers"],
    "Computer Vision": ["Computer Vision", "Neural Networks", "PyTorch", "Machine Learning"],
    "NLP & LLMs": ["NLP", "Transformers", "LLMs", "Python"],
    "Generative AI": ["LLMs", "RAG", "Transformers", "NLP"],
    "Agentic AI": ["AI Agents", "RAG", "LLMs", "Python"],
    "AI Product": ["AI Strategy", "AI Products", "Analytics", "Experimentation"],
    "Forecasting": ["Time Series", "Statistics", "Machine Learning", "Python"],
})


_BUNDLE_ALIASES: dict[str, tuple[str, ...]] = {
    "Python Engineering": ("python", "programming", "software", "api", "automation"),
    "Data Analytics": ("data analytics", "data analysis", "analytics", "power bi", "dashboard", "visualization"),
    "Data Science": ("data science", "statistics", "statistical"),
    "Machine Learning": ("machine learning", "ml engineer", "classical ml"),
    "Deep Learning": ("deep learning", "pytorch", "neural network"),
    "Computer Vision": ("computer vision", "image classification", "object detection", "vision"),
    "NLP & LLMs": ("natural language", "nlp", "transformer", "large language model"),
    "Generative AI": ("generative ai", "genai", "rag", "retrieval augmented", "llm"),
    "Agentic AI": ("agentic", "ai agent", "multi-agent", "langgraph", "tool-using agent"),
    "AI Product": ("ai product", "product management", "ai strategy"),
    "Forecasting": ("time series", "forecast", "demand planning"),
}


def canonical_skills(skill_bundle: str) -> list[str]:
    """Return a copy so callers cannot mutate the shared taxonomy."""
    return list(SKILL_BUNDLES[skill_bundle])


def infer_skill_bundles(text: str, limit: int = 3) -> list[str]:
    """Map free-text searches/goals into the managed bundle vocabulary."""
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).split())
    matches: list[tuple[int, str]] = []
    for bundle, aliases in _BUNDLE_ALIASES.items():
        normalized_aliases = [" ".join(re.sub(r"[^a-z0-9]+", " ", alias).split()) for alias in aliases]
        score = sum(len(alias.split()) for alias in normalized_aliases if f" {alias} " in f" {normalized} ")
        normalized_bundle = " ".join(re.sub(r"[^a-z0-9]+", " ", bundle.lower()).split())
        if f" {normalized_bundle} " in f" {normalized} ":
            score += 4
        if score:
            matches.append((score, bundle))
    return [bundle for _, bundle in sorted(matches, key=lambda item: (-item[0], item[1]))[:limit]]


def resolve_skill_bundle(title: str, category: str, skills: list[str] | None = None) -> str:
    """Choose one canonical bundle for catalog input without inventing new labels."""
    inferred = infer_skill_bundles(" ".join([title, category, *(skills or [])]), limit=1)
    return inferred[0] if inferred else "Python Engineering"
