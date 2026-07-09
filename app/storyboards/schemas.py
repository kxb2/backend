from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ImageModel = Literal["gpt_image", "gemini_3_5_flash_image"]


class StoryboardCreateResponse(BaseModel):
    storyboard_id: int
    generation_id: int
    status: str


class ReferenceImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    image_url: str


class StoryboardDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scenario_text: str
    genre: str
    style: str | None
    tone: str | None
    aspect_ratio: str | None
    era: str | None
    image_model: str
    reference_images: list[ReferenceImageOut]
    created_at: datetime


class StoryboardPromptResponse(BaseModel):
    storyboard_id: int
    integrated_prompt: str | None