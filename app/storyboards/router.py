from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.db.session import get_db
from app.storyboards import service
from app.storyboards.schemas import (
    ImageModel,
    StoryboardCreateResponse,
    StoryboardDetailResponse,
    StoryboardPromptResponse,
)

router = APIRouter(prefix="/storyboards", tags=["storyboards"])


@router.post("", response_model=StoryboardCreateResponse, status_code=201)
def create_storyboard(
    scenario_text: Annotated[str, Form()],
    genre: Annotated[str, Form()],
    style: Annotated[str | None, Form()] = None,
    tone: Annotated[str | None, Form()] = None,
    aspect_ratio: Annotated[str | None, Form()] = None,
    era: Annotated[str | None, Form()] = None,
    image_model: Annotated[ImageModel, Form()] = ImageModel.GPT_IMAGE,
    reference_images: Annotated[list[UploadFile | str], File()] = [],
    db: Session = Depends(get_db),
) -> StoryboardCreateResponse:
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

    return StoryboardCreateResponse(
        storyboard_id=storyboard.id,
        generation_id=generation.id,
        status=generation.status,
    )


@router.get("/{storyboard_id}", response_model=StoryboardDetailResponse)
def get_storyboard(storyboard_id: int, db: Session = Depends(get_db)):
    storyboard = service.get_storyboard(db, storyboard_id)
    if storyboard is None:
        raise HTTPException(status_code=404, detail="storyboard not found")
    return storyboard


@router.get("/{storyboard_id}/prompt", response_model=StoryboardPromptResponse)
def get_storyboard_prompt(storyboard_id: int, db: Session = Depends(get_db)) -> StoryboardPromptResponse:
    storyboard = service.get_storyboard(db, storyboard_id)
    if storyboard is None:
        raise HTTPException(status_code=404, detail="storyboard not found")
    return StoryboardPromptResponse(
        storyboard_id=storyboard.id, integrated_prompt=storyboard.integrated_prompt
    )
