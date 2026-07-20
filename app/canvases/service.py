from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core import storage
from app.core.enums import CanvasElementType
from app.canvases.models import Canvas, CanvasConnection, CanvasElement
from app.canvases.schemas import (
    CanvasConnectionIn,
    CanvasConnectionOut,
    CanvasDetailResponse,
    CanvasElementIn,
    CanvasElementOut,
)
from app.generations.models import Cut, Generation
from app.storyboards.models import Storyboard

# 유저/인증 붙기 전까지 GET /canvases가 전체 캔버스를 무제한 노출하지 않도록 하드캡.
# user_id가 생기면 이 캡보다 먼저 `WHERE user_id == current_user.id` 필터 추가하렴
DEFAULT_LIST_LIMIT = 100

# storyboard_id/cut_id 참조는 IMAGE와 MEMO(추후 연동 가능성 대비)만 허용.
_STORYBOARD_LINKABLE_TYPES = {CanvasElementType.IMAGE, CanvasElementType.MEMO}


class CanvasNotFound(Exception):
    """존재하지 않는 canvas_id로 요청한 경우"""
# 이런 내용 없는 exception 애들은 함수 안에서 언제 던지는지 결정함


class StoryboardNotFound(Exception):
    """존재하지 않는 storyboard_id를 캔버스에 연결하려는 경우"""


class InvalidStoryboardReference(Exception):
    """IMAGE/MEMO 요소가 이 캔버스에 연결된 storyboard_id 소속이 아닌
    storyboard_id/cut_id를 참조하는 경우"""
# 이것도 일단은 캔버스: 스토리보드를 1:1 대응이라고 전제하고 설계

class InvalidClientKeyReference(Exception):
    """connection/parent_client_key가 이번 저장 요청에 없는 client_key를 참조하거나,
    client_key가 중복되거나, parent 참조가 순환 구조를 만드는 경우"""


class UnsupportedAttachmentType(Exception):
    """이미지/영상으로 허용된 content-type이 아닌 파일을 첨부하려는 경우"""


def _ensure_storyboard_exists(db: Session, storyboard_id: int) -> None:
    """스토리보드 연결된다면, 스토리보드 id 진짜 있나 체크"""
    if db.query(Storyboard.id).filter(Storyboard.id == storyboard_id).first() is None:
        raise StoryboardNotFound()


def create_canvas(db: Session, storyboard_id: int | None) -> Canvas:
    """빈 캔버스 생성"""
    if storyboard_id is not None:
        _ensure_storyboard_exists(db, storyboard_id)

    canvas = Canvas(storyboard_id=storyboard_id)
    db.add(canvas)
    db.commit()
    return canvas


def list_canvases(db: Session, limit: int = DEFAULT_LIST_LIMIT) -> list[Canvas]:
    """캔버스 전체 목록(요약) 조회 — 최신 수정순, 최대 limit개"""
    return db.query(Canvas).order_by(Canvas.updated_at.desc()).limit(limit).all()


def get_canvas(db: Session, canvas_id: int) -> Canvas | None:
    """캔버스 개별 조회"""
    return db.get(Canvas, canvas_id)


def delete_canvas(db: Session, canvas_id: int) -> None:
    """캔버스 삭제: 캔버스가 소유한 R2 첨부파일(사진/영상/썸네일)도 함께 정리.

    ㅡ 순서: DB 삭제 → R2 삭제
    """
    canvas = db.get(Canvas, canvas_id)
    if canvas is None:
        raise CanvasNotFound()

    urls = _collect_attachment_urls(db, canvas_id)

    db.delete(canvas)
    db.commit()

    for url in urls:
        if storage.is_canvas_attachment_url(url):
            storage.delete_file(url)


def save_canvas(
    db: Session,
    canvas_id: int,
    *,
    storyboard_id: int | None,
    elements: list[CanvasElementIn],
    connections: list[CanvasConnectionIn],
) -> tuple[Canvas, dict[int, str]]:
    """캔버스 저장 (전체 교체) — 기존꺼 지우고 요청 목록으로 재생성.

    ㅡ elements/connections는 client_key(프론트가 붙인 임시 문자열 id)로 서로를 참조하고,
      백엔드가 새로 발급된 DB id로 매핑해서 저장.
    ㅡ 같은 canvas_id로 동시에 여러 저장 요청 들어와도 줄세울수있게 canvas row 잠금.
    """
    canvas = db.query(Canvas).filter(Canvas.id == canvas_id).with_for_update().first()
    if canvas is None:
        raise CanvasNotFound()

    if storyboard_id is not None:
        _ensure_storyboard_exists(db, storyboard_id)
        canvas.storyboard_id = storyboard_id
    effective_storyboard_id = canvas.storyboard_id

    _validate_client_keys(elements)
    _validate_storyboard_references(db, elements, effective_storyboard_id)

    old_urls = _collect_attachment_urls(db, canvas_id)

    db.query(CanvasElement).filter(CanvasElement.canvas_id == canvas_id).delete(synchronize_session=False)
    db.flush()

    new_rows = [
        CanvasElement(
            canvas_id=canvas_id,
            type=element.type,
            x=element.x,
            y=element.y,
            width=element.width,
            height=element.height,
            rotation=element.rotation,
            content_url=element.content_url,
            thumbnail_url=element.thumbnail_url,
            memo_title=element.memo_title,
            memo_content=element.memo_content,
            memo_color=element.memo_color,
            storyboard_id=element.storyboard_id,
            cut_id=element.cut_id,
        )
        for element in elements
    ]
    db.add_all(new_rows)
    db.flush()  # id(CanvasElement 요소 하나하나의 PK) 발급

    client_key_to_id = {element.client_key: row.id for element, row in zip(elements, new_rows)}

    for element, row in zip(elements, new_rows):
        if element.parent_client_key is None:
            continue
        parent_id = client_key_to_id.get(element.parent_client_key)
        if parent_id is None:
            raise InvalidClientKeyReference()
        row.parent_element_id = parent_id

    for connection in connections:
        from_id = client_key_to_id.get(connection.from_client_key)
        to_id = client_key_to_id.get(connection.to_client_key)
        if from_id is None or to_id is None:
            raise InvalidClientKeyReference()
        if from_id == to_id:
            continue  # 자기 자신에게 연결하는 노드(선)연결은 무시함
        db.add(CanvasConnection(canvas_id=canvas_id, from_element_id=from_id, to_element_id=to_id))

    db.commit()
    db.refresh(canvas)

    new_urls = {element.content_url for element in elements if element.content_url}
    new_urls |= {element.thumbnail_url for element in elements if element.thumbnail_url}
    for url in old_urls - new_urls:
        if storage.is_canvas_attachment_url(url):
            storage.delete_file(url)

    id_to_client_key = {row_id: client_key for client_key, row_id in client_key_to_id.items()}
    return canvas, id_to_client_key


def _collect_attachment_urls(db: Session, canvas_id: int) -> set[str]:
    """캔버스 저장이나 삭제할때, 없애도 되는 옛날 url들 비교해주는 조회헬퍼함수"""
    urls: set[str] = set()
    for content_url, thumbnail_url in db.query(CanvasElement.content_url, CanvasElement.thumbnail_url).filter(
        CanvasElement.canvas_id == canvas_id
    ):
        if content_url:
            urls.add(content_url)
        if thumbnail_url:
            urls.add(thumbnail_url)
    return urls


def _validate_client_keys(elements: list[CanvasElementIn]) -> None:
    """client_key 중복 검증 + parent_client_key 자기참조/순환참조 검증."""
    client_keys = [element.client_key for element in elements]
    if len(client_keys) != len(set(client_keys)):
        raise InvalidClientKeyReference()

    parent_by_key = {element.client_key: element.parent_client_key for element in elements}
    for start in parent_by_key:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                raise InvalidClientKeyReference()
            seen.add(current)
            current = parent_by_key.get(current)


# 일단 여기도 캔버스:스토리보드를 1:1 대응으로 생각하고 설계
def _validate_storyboard_references(
    db: Session, elements: list[CanvasElementIn], effective_storyboard_id: int | None
) -> None:
    """IMAGE/MEMO 타입 요소는
    이 캔버스에 연결된 storyboard_id 소속의 storyboard_id/cut_id만 참조 가능"""
    linked_elements = [e for e in elements if e.type in _STORYBOARD_LINKABLE_TYPES]
    cut_ids = {e.cut_id for e in linked_elements if e.cut_id is not None}
    storyboard_ids = {e.storyboard_id for e in linked_elements if e.storyboard_id is not None}

    if not cut_ids and not storyboard_ids:
        return
    if effective_storyboard_id is None:
        raise InvalidStoryboardReference()
    if storyboard_ids - {effective_storyboard_id}:
        raise InvalidStoryboardReference()
    if cut_ids:
        valid_count = (
            db.query(Cut.id)
            .filter(Cut.id.in_(cut_ids), Cut.storyboard_id == effective_storyboard_id)
            .count()
        )
        if valid_count != len(cut_ids):
            raise InvalidStoryboardReference()


def upload_attachment(
    db: Session, canvas_id: int, file: UploadFile, thumbnail: UploadFile | None
) -> tuple[str, str | None, CanvasElementType]:
    """캔버스 이미지/영상 업로드 — R2에 올리고 (content_url, thumbnail_url, type)만 반환.
    DB에는 X(요소 row 생성은 이후 PUT 저장 시점에 이 url을 담아서 처리)."""
    canvas = db.get(Canvas, canvas_id)
    if canvas is None:
        raise CanvasNotFound()

    if file.content_type in storage.ALLOWED_IMAGE_CONTENT_TYPES:
        data = storage.validate_image(file)
        content_url = storage.upload_image_bytes(data, file.content_type, folder="canvas-attachments")
        return content_url, None, CanvasElementType.IMAGE

    if file.content_type in storage.ALLOWED_VIDEO_CONTENT_TYPES:
        data = storage.validate_video(file)
        content_url = storage.upload_video_bytes(data, file.content_type, folder="canvas-attachments")
        thumbnail_url = None
        if thumbnail is not None:
            thumbnail_data = storage.validate_image(thumbnail)
            thumbnail_url = storage.upload_image_bytes(
                thumbnail_data, thumbnail.content_type, folder="canvas-thumbnails"
            )
        return content_url, thumbnail_url, CanvasElementType.VIDEO

    raise UnsupportedAttachmentType()


def build_canvas_detail(
    db: Session, canvas: Canvas, client_key_map: dict[int, str] | None = None
) -> CanvasDetailResponse:
    """GET, PUT(저장) 응답 양쪽에서 재사용하는 '캔버스 요소/연결 목록 조립 함수'
    ㅡ 이미지가 스토리보드 컷이랑 연결되어있으면 이미지 조인도 함"""
    client_key_map = client_key_map or {}
    elements = db.query(CanvasElement).filter(CanvasElement.canvas_id == canvas.id).all()
    connections = db.query(CanvasConnection).filter(CanvasConnection.canvas_id == canvas.id).all()

    image_elements = [e for e in elements if e.type == CanvasElementType.IMAGE]
    cut_ids = {e.cut_id for e in image_elements if e.cut_id is not None}
    grid_storyboard_ids = {
        e.storyboard_id for e in image_elements if e.cut_id is None and e.storyboard_id is not None
    }

    cut_image_by_id: dict[int, str | None] = {}
    if cut_ids:
        cut_image_by_id = dict(db.query(Cut.id, Cut.image_url).filter(Cut.id.in_(cut_ids)))

    grid_image_by_storyboard_id: dict[int, str | None] = {}
    if grid_storyboard_ids:
        grid_image_by_storyboard_id = dict(
            db.query(Generation.storyboard_id, Generation.grid_image_url).filter(
                Generation.storyboard_id.in_(grid_storyboard_ids)
            )
        )

    element_outs = []
    for element in elements:
        content_url = element.content_url
        if element.type == CanvasElementType.IMAGE:
            if element.cut_id is not None:
                content_url = cut_image_by_id.get(element.cut_id, content_url)
            elif element.storyboard_id is not None:
                content_url = grid_image_by_storyboard_id.get(element.storyboard_id, content_url)

        element_outs.append(
            CanvasElementOut(
                id=element.id,
                client_key=client_key_map.get(element.id),
                type=element.type,
                x=element.x,
                y=element.y,
                width=element.width,
                height=element.height,
                rotation=element.rotation,
                content_url=content_url,
                thumbnail_url=element.thumbnail_url,
                memo_title=element.memo_title,
                memo_content=element.memo_content,
                memo_color=element.memo_color,
                storyboard_id=element.storyboard_id,
                cut_id=element.cut_id,
                parent_element_id=element.parent_element_id,
            )
        )

    connection_outs = [
        CanvasConnectionOut(id=c.id, from_element_id=c.from_element_id, to_element_id=c.to_element_id)
        for c in connections
    ]

    return CanvasDetailResponse(
        id=canvas.id,
        storyboard_id=canvas.storyboard_id,
        elements=element_outs,
        connections=connection_outs,
        created_at=canvas.created_at,
        updated_at=canvas.updated_at,
    )
