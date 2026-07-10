from datetime import datetime

from app.core.enums import Genre, ImageModel, JobStatus
from app.core.schema import CamelModel

__all__ = [
    "Genre",
    "ImageModel",
    "ReferenceImageOut",
    "StoryboardCreateResponse",
    "StoryboardDetailResponse",
    "StoryboardPromptResponse",
]


class StoryboardCreateResponse(CamelModel):
    storyboard_id: int
    generation_id: int
    status: JobStatus


class ReferenceImageOut(CamelModel):
    id: int
    image_url: str


class StoryboardDetailResponse(CamelModel):
    id: int
    scenario_text: str
    genre: Genre
    style: str | None
    tone: str | None
    aspect_ratio: str | None
    era: str | None
    image_model: ImageModel
    reference_images: list[ReferenceImageOut]
    created_at: datetime


class StoryboardPromptResponse(CamelModel):
    storyboard_id: int
    integrated_prompt: str | None
