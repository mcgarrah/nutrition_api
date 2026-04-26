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
    segments: list[SegmentItem] = Field(default_factory=list)
    families: list[FamilyItem] = Field(default_factory=list)
    classes: list[ClassItem] = Field(default_factory=list)
    bricks: list[BrickItem] = Field(default_factory=list)
