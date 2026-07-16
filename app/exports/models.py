from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ExportType, JobStatus, enum_values
from app.db.base import Base

_status_type = Enum(
    JobStatus, native_enum=False, length=20, validate_strings=True, values_callable=enum_values
)
_type_type = Enum(
    ExportType, native_enum=False, length=20, validate_strings=True, values_callable=enum_values
)


# PDF/이미지 Export 작업 추적
class Export(Base):
    __tablename__ = "exports"

    id: Mapped[int] = mapped_column(primary_key=True)
    storyboard_id: Mapped[int] = mapped_column(
        ForeignKey("storyboards.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[ExportType] = mapped_column(_type_type)
    include_individual_cuts: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[JobStatus] = mapped_column(_status_type, default=JobStatus.PENDING)
    download_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
