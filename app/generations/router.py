from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.generations import service
from app.generations.schemas import GenerationDetailResponse

router = APIRouter(prefix="/generations", tags=["generations"])


@router.get("/{generation_id}", response_model=GenerationDetailResponse)
def get_generation(generation_id: int, db: Session = Depends(get_db)) -> GenerationDetailResponse:
    """9컷 생성 상태/결과 조회"""
    generation = service.get_generation(db, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="generation not found")

    return GenerationDetailResponse(
        id=generation.id,
        storyboard_id=generation.storyboard_id,
        status=generation.status,
        grid_image_url=generation.grid_image_url,
        cuts=generation.storyboard.cuts,
    )
