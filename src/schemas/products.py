from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.services.skill_taxonomy import SKILL_BUNDLES


class ProductInput(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=20, max_length=5000)
    category: str = Field(min_length=2, max_length=80)
    difficulty: str
    price: Decimal = Field(ge=0, le=1_000_000)
    original_price: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    duration_hours: float = Field(gt=0, le=1000)
    skill_bundle: str | None = None
    skills: list[str] = Field(default_factory=list, max_length=20)
    content_sections: list[str] | None = Field(default=None, max_length=4)
    tags: list[str] = Field(default_factory=list, max_length=20)
    image_url: str = Field(default="", max_length=500)
    is_bundle: bool | None = None
    bundled_product_ids: list[str] | None = Field(default=None, max_length=20)
    prerequisite_product_ids: list[str] | None = Field(default=None, max_length=20)
    career_outcomes: list[str] | None = Field(default=None, max_length=12)
    social_proof_text: str | None = Field(default=None, max_length=500)
    promotion_ends_at: datetime | None = None

    @field_validator("difficulty")
    @classmethod
    def valid_difficulty(cls, value: str) -> str:
        value = value.lower()
        if value not in {"beginner", "intermediate", "advanced", "all-levels"}:
            raise ValueError("invalid difficulty")
        return value

    @field_validator("skill_bundle")
    @classmethod
    def valid_skill_bundle(cls, value: str | None) -> str | None:
        if value is not None and value not in SKILL_BUNDLES:
            raise ValueError("skill bundle must use the managed taxonomy")
        return value

    @field_validator("content_sections")
    @classmethod
    def valid_content_sections(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        sections = [section.strip() for section in value if section.strip()]
        if sections and len(sections) < 3:
            raise ValueError("course content must contain at least three paragraphs")
        if any(len(section) > 2000 for section in sections):
            raise ValueError("course content paragraphs must be 2,000 characters or fewer")
        return sections

    @model_validator(mode="after")
    def valid_discount(self):
        if self.original_price is not None and self.original_price < self.price:
            raise ValueError("original price must be greater than or equal to the sale price")
        if self.is_bundle and not self.bundled_product_ids:
            raise ValueError("a bundle must include at least one product")
        return self
