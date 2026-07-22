# 캔버스 기능
# ㅡ 포인터(선택), 뷰어포인터(화면이동), 이미지 첨부, 동영상(1개) 첨부,
#    노드(선 연결), 그룹화 섹션, 메모
# ㅡ R2에 캔버스에 첨부하는 이미지, 동영상도 올라감
#
# 미확정:
# - 드로잉 기능 확정되면 type에 drawing 추가 + drawing_data 필드 사용
#   drawing_data(JSONB): 벡터 좌표/경로 데이터 저장
# - 특정 요소에 붙는 댓글/ 일반 텍스트는 메모가 대체할것같아서 보류
# - 컷별 프롬프트가 연결된 메모/ 컷별 이미지가 연결된 이미지 가져오기
#   (스토리보드와 연동될 가능성? 백 DB/API 이미 있어서 프론트에서 갖다쓰면됨)
#   + 다만 재생성된 컷들도 자동 연동되게 하려면 로직 따로 짜야함
# - ui/ux분이 누크처럼 미니맵 기능 제안하쎴음(시간여유있거나 프론트구현가능하면)

from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import CanvasElementType, enum_values
from app.db.base import Base

_element_type_type = Enum(
    CanvasElementType, native_enum=False, length=20, validate_strings=True, values_callable=enum_values
)


class Canvas(Base):
    __tablename__ = "canvases"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200))
    storyboard_id: Mapped[int | None] = mapped_column(
        ForeignKey("storyboards.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    elements: Mapped[list["CanvasElement"]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan"
    )
    connections: Mapped[list["CanvasConnection"]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan"
    )


class CanvasElement(Base):
    __tablename__ = "canvas_elements"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[int] = mapped_column(ForeignKey("canvases.id", ondelete="CASCADE"), index=True)
    type: Mapped[CanvasElementType] = mapped_column(_element_type_type)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    rotation: Mapped[float | None] = mapped_column(Float)
    content_url: Mapped[str | None] = mapped_column(String(500))
    thumbnail_url: Mapped[str | None] = mapped_column(String(500))
    memo_title: Mapped[str | None] = mapped_column(String(200))
    memo_content: Mapped[str | None] = mapped_column(Text)
    memo_color: Mapped[str | None] = mapped_column(String(20))
    # 스토리보드 id, 컷 id는 IMAGE, MEMO 타입만 일단 허용(추후 연동 대비)
    storyboard_id: Mapped[int | None] = mapped_column(
        ForeignKey("storyboards.id", ondelete="SET NULL"), nullable=True
    )
    cut_id: Mapped[int | None] = mapped_column(ForeignKey("cuts.id", ondelete="SET NULL"), nullable=True)
    parent_element_id: Mapped[int | None] = mapped_column(
        ForeignKey("canvas_elements.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    canvas: Mapped["Canvas"] = relationship(back_populates="elements")


class CanvasConnection(Base):  # 노드(선) 연결
    __tablename__ = "canvas_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[int] = mapped_column(ForeignKey("canvases.id", ondelete="CASCADE"), index=True)
    from_element_id: Mapped[int] = mapped_column(ForeignKey("canvas_elements.id", ondelete="CASCADE"))
    to_element_id: Mapped[int] = mapped_column(ForeignKey("canvas_elements.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    canvas: Mapped["Canvas"] = relationship(back_populates="connections")
