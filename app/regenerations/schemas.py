from app.core.enums import JobStatus
from app.core.schema import CamelModel

__all__ = ["RegenerationCreateResponse", "RegenerationDetailResponse"]


class RegenerationCreateResponse(CamelModel):
    regeneration_id: int
    status: JobStatus


class RegenerationDetailResponse(CamelModel):
    id: int
    cut_id: int
    status: JobStatus
    image_url: str | None
    error_message: str | None
