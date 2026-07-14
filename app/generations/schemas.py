from app.core.enums import JobStatus
from app.core.schema import CamelModel

__all__ = ["CutOut", "GenerationDetailResponse"]


class CutOut(CamelModel):
    id: int
    order_no: int
    prompt_text: str | None
    angle_type: str | None
    image_url: str | None
    status: JobStatus


class GenerationDetailResponse(CamelModel):
    id: int
    storyboard_id: int
    status: JobStatus
    grid_image_url: str | None
    cuts: list[CutOut]
