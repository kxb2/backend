from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

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
    reference_images: Annotated[list[UploadFile], File()] = [],
    db: Session = Depends(get_db),
) -> StoryboardCreateResponse:
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
            reference_images=reference_images,
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
