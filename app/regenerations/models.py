# 지금은 기존 컷별 프롬프트 그대로 컷만 다시 생성
# 추후 사용자의 재명령도 고려중 ㅡ 근데 이건 로직도 새로 짜야함. 어댑터도 또 부르나?
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import JobStatus, enum_values
from app.db.base import Base

if TYPE_CHECKING:
    from app.generations.models import Cut

_status_type = Enum(
    JobStatus, native_enum=False, length=20, validate_strings=True, values_callable=enum_values
)


# 특정 컷 재생성 작업 추적
class Regeneration(Base):
    __tablename__ = "regenerations"

    id: Mapped[int] = mapped_column(primary_key=True)
    cut_id: Mapped[int] = mapped_column(ForeignKey("cuts.id", ondelete="CASCADE"), index=True)
    status: Mapped[JobStatus] = mapped_column(_status_type, default=JobStatus.PENDING)
    image_url: Mapped[str | None] = mapped_column(String(500))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cut: Mapped["Cut"] = relationship()
