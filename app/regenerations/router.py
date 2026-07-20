from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.regenerations import service
from app.regenerations.schemas import RegenerationDetailResponse

router = APIRouter(prefix="/regenerations", tags=["regenerations"])


@router.get("/{regeneration_id}", response_model=RegenerationDetailResponse)
def get_regeneration(regeneration_id: int, db: Session = Depends(get_db)) -> RegenerationDetailResponse:
    """특정 컷 재생성 작업의 진행 status와 결과를 조회."""
    regeneration = service.get_regeneration(db, regeneration_id)
    if regeneration is None:
        raise HTTPException(status_code=404, detail="regeneration not found")

    return regeneration
