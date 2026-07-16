"""
GS1 Global Product Classification (GPC) Pydantic models.

Mirrors the hierarchy from the shiny-shop Django implementation:
Segments -> Families -> Classes -> Bricks -> AttributeTypes -> AttributeValues

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
from pydantic import BaseModel, Field


# --- List models (compact) ---

class SegmentItem(BaseModel):
    segment_code: str
    description: str | None = None


class FamilyItem(BaseModel):
    family_code: str
    description: str | None = None


class ClassItem(BaseModel):
    class_code: str
    description: str | None = None


class BrickItem(BaseModel):
    brick_code: str
    description: str | None = None


class AttributeValueItem(BaseModel):
    att_value_code: str
    att_value_text: str | None = None


class AttributeTypeItem(BaseModel):
    att_type_code: str
    att_type_text: str | None = None
    values: list[AttributeValueItem] = Field(default_factory=list)


class AttributeMatch(BaseModel):
    """A search hit on an attribute type or value, and the bricks it lives under.

    Attributes are where the specificity lives: "olive oil" is not a brick but
    the value "OLIVE OIL" of the attribute "Type of Edible Vegetable or Plant
    Oil" on the brick "Oils Edible - Vegetable or Plant". Searching only brick
    descriptions can never find it, so a match carries the bricks that hold the
    attribute — the caller's route back into the hierarchy.
    """

    kind: str = Field(description="'value' or 'type' — what the query matched")
    att_type_code: str
    att_type_text: str | None = None
    att_value_code: str | None = Field(
        default=None, description="Set when kind == 'value'"
    )
    att_value_text: str | None = None
    bricks: list[BrickItem] = Field(
        default_factory=list, description="Bricks that carry this attribute"
    )


# --- Detail models (with nested children and breadcrumbs) ---

class SegmentDetail(BaseModel):
    segment_code: str
    description: str | None = None
    families: list[FamilyItem] = Field(default_factory=list)


class ParentSegmentRef(BaseModel):
    segment_code: str
    segment_description: str | None = None


class FamilyDetail(BaseModel):
    family_code: str
    description: str | None = None
    segment_code: str | None = None
    segment_code_details: ParentSegmentRef | None = None
    full_path: str | None = None
    classes: list[ClassItem] = Field(default_factory=list)


class ParentFamilyRef(BaseModel):
    family_code: str
    description: str | None = None
    segment_code: str | None = None
    segment_description: str | None = None


class ClassDetail(BaseModel):
    class_code: str
    description: str | None = None
    family_code: str | None = None
    family_code_details: ParentFamilyRef | None = None
    full_path: str | None = None
    bricks: list[BrickItem] = Field(default_factory=list)


class ParentClassRef(BaseModel):
    class_code: str
    description: str | None = None
    family_code: str | None = None
    family_description: str | None = None
    segment_code: str | None = None
    segment_description: str | None = None


class BrickDetail(BaseModel):
    brick_code: str
    description: str | None = None
    class_code: str | None = None
    class_code_details: ParentClassRef | None = None
    full_path: str | None = None
    attributes: list[AttributeTypeItem] = Field(default_factory=list)


# --- Paginated response ---

class PaginatedResponse(BaseModel):
    count: int
    next: str | None = None
    previous: str | None = None
    results: list = Field(default_factory=list)


# --- Search response ---

class SearchResponse(BaseModel):
    """Cross-entity search results, capped per entity type.

    `counts` reports how many rows actually matched, so a caller can tell that
    the lists were truncated instead of silently receiving a slice and
    believing it was the whole answer.
    """

    segments: list[SegmentItem] = Field(default_factory=list)
    families: list[FamilyItem] = Field(default_factory=list)
    classes: list[ClassItem] = Field(default_factory=list)
    bricks: list[BrickItem] = Field(default_factory=list)
    attributes: list[AttributeMatch] = Field(
        default_factory=list,
        description="Attribute types/values matching the query, with their bricks",
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Total matches per entity type, before the limit is applied",
    )
    truncated: bool = Field(
        default=False,
        description="True when at least one entity type had more matches than the limit",
    )


# --- Curated FDC-category -> GPC mapping viewer ------------------------

class CuratedMapping(BaseModel):
    """One hand-verified entry from gpc_match.py's curated tables."""

    category: str = Field(description="FDC branded_food_category, exact string")
    level: str = Field(description="'brick' (specific) or 'class' (coarser)")
    code: str = Field(description="The matched GPC brick_code or class_code")
    hierarchy: list[str] = Field(
        default_factory=list,
        description="Segment > Family > Class[ > Brick], resolved from the code",
    )


class UncoveredCategory(BaseModel):
    """An FDC category with no curated mapping at either level, ranked by size."""

    category: str
    food_count: int


class MappingCoverage(BaseModel):
    """How much of the real local FDC corpus the curated tables reach.

    Measured live against the local FDC bulk copy, not asserted — the number
    here and the number in ARCH.md are computed the same way and cannot drift
    apart the way a hand-maintained doc comment can.
    """

    total_categorized_foods: int
    covered_foods: int
    coverage_pct: float
    distinct_fdc_categories: int
    curated_brick_entries: int
    curated_class_entries: int
    uncovered_categories: list[UncoveredCategory] = Field(
        default_factory=list,
        description="FDC categories with no curated mapping, largest first",
    )


class MappingsResponse(BaseModel):
    mappings: list[CuratedMapping] = Field(default_factory=list)
    coverage: MappingCoverage | None = Field(
        default=None,
        description="None when the local FDC bulk copy isn't available to measure against",
    )
