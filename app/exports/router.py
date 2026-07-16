from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.exports import service
from app.exports.schemas import ExportDetailResponse

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get("/{export_id}", response_model=ExportDetailResponse)
def get_export(export_id: int, db: Session = Depends(get_db)) -> ExportDetailResponse:
    """Export 결과 조회 (완료 여부·다운로드 링크)"""
    export = service.get_export(db, export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="export not found")
    return export
