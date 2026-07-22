from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import JobStatus, enum_values
from app.db.base import Base

if TYPE_CHECKING:
    from app.storyboards.models import Storyboard

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
    error_message: Mapped[str | None] = mapped_column(Text)
    # 통합 프롬프트 글자수/형식 검증 실패로 재생성한 횟수 (1이 재시도 없는 정상).
    # call_with_retry의 타임아웃 재시도와는 별개 — 이건 로그만
    prompt_attempt_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    storyboard: Mapped["Storyboard"] = relationship(back_populates="generation")


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
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    storyboard: Mapped["Storyboard"] = relationship(back_populates="cuts")
