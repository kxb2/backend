from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.db.session import get_db
from app.exports import service as exports_service
from app.exports.schemas import ExportCreateResponse, ImageExportRequest
from app.exports.service import run_image_export, run_pdf_export
from app.generations.service import run_generation
from app.regenerations import service as regenerations_service
from app.regenerations.schemas import RegenerationCreateResponse
from app.regenerations.service import run_regeneration
from app.storyboards import service
from app.storyboards.schemas import (
    Genre,
    ImageModel,
    StoryboardCreateResponse,
    StoryboardDetailResponse,
    StoryboardPromptResponse,
)

router = APIRouter(prefix="/storyboards", tags=["storyboards"])


@router.post("", response_model=StoryboardCreateResponse, status_code=201)
def create_storyboard(
    scenario_text: Annotated[str, Form(min_length=1)],
    genre: Annotated[Genre, Form()],
    background_tasks: BackgroundTasks,
    style: Annotated[str | None, Form()] = None,
    tone: Annotated[str | None, Form()] = None,
    aspect_ratio: Annotated[str | None, Form()] = None,
    era: Annotated[str | None, Form()] = None,
    image_model: Annotated[ImageModel, Form()] = ImageModel.GPT_IMAGE,
    reference_images: Annotated[list[UploadFile | str], File()] = [],
    db: Session = Depends(get_db),
) -> StoryboardCreateResponse:
    """스토리보드 생성"""
    # Swagger UI 등 일부 클라이언트가 파일 미선택 시 빈 문자열을 보냄 — 그 경우만 "파일 없음"으로 처리.
    # fastapi.UploadFile은 starlette.datastructures.UploadFile의 서브클래스라, 파일 개수가 많을 때
    # FastAPI가 서브클래스로 감싸지 않고 부모 클래스 그대로 넘겨주는 경우가 있어 부모 클래스로 검사한다.
    uploaded_files: list[UploadFile] = []
    for image in reference_images:
        if isinstance(image, StarletteUploadFile):
            uploaded_files.append(image)
        elif image != "":
            raise HTTPException(status_code=400, detail="레퍼런스 이미지 형식이 올바르지 않습니다.")

    try:
        storyboard, generation = service.create_storyboard(
            db,
            scenario_text=scenario_text,
            genre=genre,
            style=style,
            tone=tone,
            aspect_ratio=aspect_ratio,
            era=era,
            image_model=image_model,
            reference_images=uploaded_files,
        )
    except service.ReferenceImageLimitExceeded as exc:
        raise HTTPException(
            status_code=400,
            detail=f"레퍼런스 이미지는 최대 {exc.limit}장까지 첨부할 수 있습니다.",
        ) from exc

    background_tasks.add_task(run_generation, storyboard.id)

    return StoryboardCreateResponse(
        storyboard_id=storyboard.id,
        generation_id=generation.id,
        status=generation.status,
    )


@router.get("/{storyboard_id}", response_model=StoryboardDetailResponse)
def get_storyboard(storyboard_id: int, db: Session = Depends(get_db)):
    """스토리보드 조회"""
    storyboard = service.get_storyboard(db, storyboard_id)
    if storyboard is None:
        raise HTTPException(status_code=404, detail="storyboard not found")
    return storyboard


@router.delete("/{storyboard_id}", status_code=204)
def delete_storyboard(storyboard_id: int, db: Session = Depends(get_db)) -> None:
    """스토리보드 삭제 — 관련 R2 파일(레퍼런스, 컷, 그리드, export)도 정리"""
    try:
        service.delete_storyboard(db, storyboard_id)
    except service.StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail="storyboard not found") from exc
    except service.GenerationInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.RegenerationInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.ExportInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{storyboard_id}/prompt", response_model=StoryboardPromptResponse)
def get_storyboard_prompt(storyboard_id: int, db: Session = Depends(get_db)) -> StoryboardPromptResponse:
    """스토리보드 통합 프롬프트 조회"""
    storyboard = service.get_storyboard(db, storyboard_id)
    if storyboard is None:
        raise HTTPException(status_code=404, detail="storyboard not found")
    return StoryboardPromptResponse(
        storyboard_id=storyboard.id, integrated_prompt=storyboard.integrated_prompt
    )


@router.post(
    "/{storyboard_id}/cuts/{cut_id}/regeneration",
    response_model=RegenerationCreateResponse,
    status_code=201,
)
def create_regeneration(
    storyboard_id: int,
    cut_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RegenerationCreateResponse:
    """특정 컷 재생성 요청 (현재 선택된 이미지 모델로, 컷별 프롬프트는 기존것 재사용)"""
    try:
        regeneration = regenerations_service.create_regeneration(db, storyboard_id, cut_id)
    except regenerations_service.StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail="storyboard not found") from exc
    except regenerations_service.CutNotFound as exc:
        raise HTTPException(status_code=404, detail="cut not found") from exc
    except regenerations_service.CutNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except regenerations_service.RegenerationInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background_tasks.add_task(run_regeneration, regeneration.id)

    return RegenerationCreateResponse(regeneration_id=regeneration.id, status=regeneration.status)


@router.post(
    "/{storyboard_id}/exports/image", response_model=ExportCreateResponse, status_code=201
)
def create_image_export(
    storyboard_id: int,
    request: ImageExportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ExportCreateResponse:
    """이미지 Export 요청 (기본: 3x3 그리드 1장, 옵션: 컷 개별 이미지 포함 zip)"""
    try:
        export = exports_service.create_image_export(
            db, storyboard_id, include_individual_cuts=request.include_individual_cuts
        )
    except exports_service.StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail="storyboard not found") from exc
    except exports_service.GenerationNotCompleted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except exports_service.ExportInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except exports_service.RegenerationInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background_tasks.add_task(run_image_export, export.id)

    return ExportCreateResponse(export_id=export.id, status=export.status)


@router.post(
    "/{storyboard_id}/exports/pdf", response_model=ExportCreateResponse, status_code=201
)
def create_pdf_export(
    storyboard_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ExportCreateResponse:
    """PDF Export 요청 (9컷 이미지 + 샷별 프롬프트가 담긴 PDF 생성)"""
    try:
        export = exports_service.create_pdf_export(db, storyboard_id)
    except exports_service.StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail="storyboard not found") from exc
    except exports_service.GenerationNotCompleted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except exports_service.ExportInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except exports_service.RegenerationInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background_tasks.add_task(run_pdf_export, export.id)

    return ExportCreateResponse(export_id=export.id, status=export.status)
