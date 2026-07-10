from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import JobStatus, enum_values
from app.db.base import Base

_status_type = Enum(
    JobStatus, native_enum=False, length=20, validate_strings=True, values_callable=enum_values
)


# 9컷 생성 작업 추적
class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[int] = mapped_column(primary_key=True)
    storyboard_id: Mapped[int] = mapped_column(
        ForeignKey("storyboards.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[JobStatus] = mapped_column(_status_type, default=JobStatus.PENDING)
    grid_image_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Cut(Base):
    __tablename__ = "cuts"
    __table_args__ = (UniqueConstraint("storyboard_id", "order_no", name="uq_cuts_storyboard_id_order_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    storyboard_id: Mapped[int] = mapped_column(
        ForeignKey("storyboards.id", ondelete="CASCADE"), index=True
    )
    order_no: Mapped[int] = mapped_column(Integer)
    prompt_text: Mapped[str | None] = mapped_column(Text)
    angle_type: Mapped[str | None] = mapped_column(String(50))
    image_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[JobStatus] = mapped_column(_status_type, default=JobStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
