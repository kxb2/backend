from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Storyboard(Base):
    __tablename__ = "storyboards"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_text: Mapped[str] = mapped_column(Text)
    genre: Mapped[str] = mapped_column(String(50))
    style: Mapped[str | None] = mapped_column(String(50))
    tone: Mapped[str | None] = mapped_column(String(50))
    aspect_ratio: Mapped[str | None] = mapped_column(String(20))
    era: Mapped[str | None] = mapped_column(String(50))
    image_model: Mapped[str] = mapped_column(String(50), default="gpt_image")
    integrated_prompt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    reference_images: Mapped[list["ReferenceImage"]] = relationship(
        back_populates="storyboard", cascade="all, delete-orphan"
    )


class ReferenceImage(Base):
    __tablename__ = "reference_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    storyboard_id: Mapped[int] = mapped_column(ForeignKey("storyboards.id"))
    image_url: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    storyboard: Mapped["Storyboard"] = relationship(back_populates="reference_images")