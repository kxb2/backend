from datetime import datetime

from pydantic import model_validator

from app.core.enums import CanvasElementType
from app.core.schema import CamelModel

_STORYBOARD_LINKABLE_TYPES = {CanvasElementType.IMAGE, CanvasElementType.MEMO}

__all__ = [
    "CanvasElementType",
    "CanvasCreateRequest",
    "CanvasCreateResponse",
    "CanvasElementIn",
    "CanvasConnectionIn",
    "CanvasSaveRequest",
    "CanvasElementOut",
    "CanvasConnectionOut",
    "CanvasDetailResponse",
    "CanvasListItem",
    "CanvasAttachmentResponse",
]


class CanvasCreateRequest(CamelModel):
    storyboard_id: int | None = None


class CanvasCreateResponse(CamelModel):
    canvas_id: int


class CanvasElementIn(CamelModel):
    client_key: str
    type: CanvasElementType
    x: float
    y: float
    width: float | None = None
    height: float | None = None
    rotation: float | None = None
    content_url: str | None = None
    thumbnail_url: str | None = None
    memo_title: str | None = None
    memo_content: str | None = None
    memo_color: str | None = None
    storyboard_id: int | None = None
    cut_id: int | None = None
    parent_client_key: str | None = None

    @model_validator(mode="after")
    def _check_storyboard_reference_type(self) -> "CanvasElementIn":
        if self.type not in _STORYBOARD_LINKABLE_TYPES and (
            self.storyboard_id is not None or self.cut_id is not None
        ):
            raise ValueError("storyboard_id/cut_id는 IMAGE, MEMO 타입에서만 사용할 수 있습니다.")
        return self


class CanvasConnectionIn(CamelModel):
    from_client_key: str
    to_client_key: str


class CanvasSaveRequest(CamelModel):
    storyboard_id: int | None = None
    elements: list[CanvasElementIn] = []
    connections: list[CanvasConnectionIn] = []


class CanvasElementOut(CamelModel):
    id: int
    # 저장(PUT) 직후 응답에서만 채워짐 — 프론트가 보낸 client_key를 실제 DB id에 매핑
    # 단순 조회(GET)에서는 이 매핑이 필요 없으므로 None.
    client_key: str | None = None
    type: CanvasElementType
    x: float
    y: float
    width: float | None
    height: float | None
    rotation: float | None
    content_url: str | None
    thumbnail_url: str | None
    memo_title: str | None
    memo_content: str | None
    memo_color: str | None
    storyboard_id: int | None
    cut_id: int | None
    parent_element_id: int | None


class CanvasConnectionOut(CamelModel):
    id: int
    from_element_id: int
    to_element_id: int


class CanvasDetailResponse(CamelModel):
    id: int
    storyboard_id: int | None
    elements: list[CanvasElementOut]
    connections: list[CanvasConnectionOut]
    created_at: datetime
    updated_at: datetime


class CanvasListItem(CamelModel):
    id: int
    storyboard_id: int | None
    created_at: datetime
    updated_at: datetime


class CanvasAttachmentResponse(CamelModel):
    content_url: str
    thumbnail_url: str | None
    type: CanvasElementType
