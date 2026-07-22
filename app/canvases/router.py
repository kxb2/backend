from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.canvases import service
from app.canvases.schemas import (
    CanvasAttachmentResponse,
    CanvasCreateRequest,
    CanvasCreateResponse,
    CanvasDetailResponse,
    CanvasListItem,
    CanvasSaveRequest,
)
from app.db.session import get_db

router = APIRouter(prefix="/canvases", tags=["canvases"])


@router.post("", response_model=CanvasCreateResponse, status_code=201)
def create_canvas(request: CanvasCreateRequest, db: Session = Depends(get_db)) -> CanvasCreateResponse:
    """빈 캔버스 생성"""
    try:
        canvas = service.create_canvas(db, request.storyboard_id)
    except service.StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail="storyboard not found") from exc
    return CanvasCreateResponse(canvas_id=canvas.id, title=canvas.title)


@router.get("", response_model=list[CanvasListItem])
def list_canvases(
    limit: int = Query(service.DEFAULT_LIST_LIMIT, ge=1, le=500), db: Session = Depends(get_db)
):
    """캔버스 전체 목록(요약) 조회 — 최신 수정순, 최대 limit개"""
    return service.list_canvases(db, limit=limit)


@router.get("/{canvas_id}", response_model=CanvasDetailResponse)
def get_canvas(canvas_id: int, db: Session = Depends(get_db)) -> CanvasDetailResponse:
    """캔버스 개별 조회 — 일반 캔버스/스토리보드연결 캔버스(build_canvas_detail 함수 사용)"""
    canvas = service.get_canvas(db, canvas_id)
    if canvas is None:
        raise HTTPException(status_code=404, detail="canvas not found")
    return service.build_canvas_detail(db, canvas)


@router.delete("/{canvas_id}", status_code=204)
def delete_canvas(canvas_id: int, db: Session = Depends(get_db)) -> None:
    """캔버스 삭제 — 캔버스가 소유한 첨부 이미지/영상(R2)도 함께 정리"""
    try:
        service.delete_canvas(db, canvas_id)
    except service.CanvasNotFound as exc:
        raise HTTPException(status_code=404, detail="canvas not found") from exc


@router.put("/{canvas_id}", response_model=CanvasDetailResponse)
def save_canvas(
    canvas_id: int, request: CanvasSaveRequest, db: Session = Depends(get_db)
) -> CanvasDetailResponse:
    """캔버스 저장 — 요소/연결 전체를 요청 내용으로 교체"""
    try:
        canvas, client_key_map = service.save_canvas(
            db,
            canvas_id,
            storyboard_id=request.storyboard_id,
            elements=request.elements,
            connections=request.connections,
        )
    except service.CanvasNotFound as exc:
        raise HTTPException(status_code=404, detail="canvas not found") from exc
    except service.StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail="storyboard not found") from exc
    except service.InvalidStoryboardReference as exc:
        raise HTTPException(
            status_code=400,
            detail="IMAGE/MEMO 요소는 이 캔버스에 연결된 스토리보드에 속한 storyboard_id/cut_id만 참조할 수 있습니다.",
        ) from exc
    except service.InvalidClientKeyReference as exc:
        raise HTTPException(
            status_code=400,
            detail="client_key가 중복되었거나, 존재하지 않는 client_key를 참조했거나, 순환 참조가 있습니다.",
        ) from exc

    return service.build_canvas_detail(db, canvas, client_key_map)
    # 일단은 캔버스: 스토리보드 연결된다면 1:1 대응으로 생각하고 설계


@router.post("/{canvas_id}/attachments", response_model=CanvasAttachmentResponse, status_code=201)
def upload_attachment(
    canvas_id: int,
    file: UploadFile = File(...),
    thumbnail: UploadFile | str | None = File(None),
    db: Session = Depends(get_db),
) -> CanvasAttachmentResponse:
    """캔버스 이미지/영상 첨부 업로드 — R2에 올리고 url만 반환(요소 저장은 이후 PUT)"""
    # Swagger UI 등 일부 클라이언트가 썸네일 미선택 시 빈 문자열을 보냄 — 그 경우만 "썸네일 없음"으로 처리.
    if thumbnail is not None and not isinstance(thumbnail, StarletteUploadFile):
        if thumbnail != "":
            raise HTTPException(status_code=400, detail="썸네일 형식이 올바르지 않습니다.")
        thumbnail = None

    try:
        content_url, thumbnail_url, element_type = service.upload_attachment(db, canvas_id, file, thumbnail)
    except service.CanvasNotFound as exc:
        raise HTTPException(status_code=404, detail="canvas not found") from exc
    except service.UnsupportedAttachmentType as exc:
        raise HTTPException(status_code=400, detail="허용되지 않는 파일 형식입니다.") from exc

    return CanvasAttachmentResponse(content_url=content_url, thumbnail_url=thumbnail_url, type=element_type)
