from app.core.enums import ExportType, JobStatus
from app.core.schema import CamelModel

__all__ = ["ExportCreateResponse", "ExportDetailResponse", "ImageExportRequest"]


class ImageExportRequest(CamelModel):
    include_individual_cuts: bool = False


class ExportCreateResponse(CamelModel):
    export_id: int
    status: JobStatus


class ExportDetailResponse(CamelModel):
    id: int
    storyboard_id: int
    type: ExportType
    status: JobStatus
    download_url: str | None
